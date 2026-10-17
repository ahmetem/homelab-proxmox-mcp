"""Read-only host command allow-list: the data and the one validator (madde 309).

SHARED FILE. It is kept BYTE-IDENTICAL in two repos:
  - pve-mcp            proxmox_mcp/read_allow.py           (the gate: proxmox_host_read_exec)
  - homelab-agent-sys  homelab_agent_sys/read_allow.py     (the agent's second layer:
                                                             safety.read_exec_reddi -> S5)
Both repos test its sha256 (CRLF folded to LF) against the same constant, so an edit
in one repo fails the other's tests until it is copied over and both constants move.
Stdlib only, no import from either package. Decision record: homelab-agent-sys
docs/decisions.md, 2026-10-16 "read_exec izin listesi".

Why an allow-list: the deny-list scanner it replaces was beaten in every review round
(madde 295, 310, 312, 316, 317) -- getopt prefixes, `-oKEY=VAL`, a value-taking option
hiding the subcommand, an unlisted log name that writes. Here nothing passes unless
this file names it:

  * program   -> a bare name from SPEC (or /usr/bin, /usr/sbin, /bin, /sbin + name)
  * subcommand-> for programs whose SPEC entry is a dict, a listed key
  * option    -> listed by its exact spelling; short and long forms are listed
                 separately; a long option is never matched by a prefix
  * operand   -> free, except the per-program rules in OPERANDS and VALUES

Option syntax inside a SPEC string (space-separated):
  -x       short flag; flags may be clustered (`-la`, `-ltnp`)
  -x:      short option with a value in the NEXT word; it may end a cluster (`-eo pid`)
  -x+      same, and the value may also be attached (`-n50`, `-A3`, `-d1`)
  -x?      short option with an OPTIONAL value, attached only (`-Iseconds`), unless
           NEXT names a pattern for a separate word (`journalctl -b -1`)
  --name   long flag (`--name=V` is refused)
  --name=  long option with a value: `--name=V` or `--name V`
  --name?  long option with an OPTIONAL value: bare, or `--name=V` (never the next word)
  -NUM     legacy `-<digits>` count (`head -5`, `tail -100`, `grep -3`)
A word is an option iff it starts with `-` and is not `-`; after `--` all are operands.
A value consumed by an option is not inspected (`--since -20min` is a value).
An unquoted glob (* ? [) is allowed only in a free operand of the host command: bash
would expand it anywhere else too, into any number of words, and a word count the
validator did not see shifts what follows (`systemctl -p s* status x` with files `sa`
and `stop` in the cwd runs `systemctl -p sa stop status x`). Option values, the program,
subcommands and every word before one, and the whole `pct exec` command are refused
with an unquoted glob; quoting passes it literally. An operand's expansions are more
operands -- never an option, as no word may start with a glob.

Shell boundary: the command is split by a quote-aware tokenizer that models the only
bash syntax left once these are refused -- unquoted ; & | < > ( ) $ ` \\ { } !, an
unquoted `#` or glob (* ? [) at the start of a word, an unquoted `~` at the start of a
word or after `=`/`:`, `$` ` \\ inside double quotes, any control character. What remains is words made of plain, '…' and "…" segments, which
bash and this tokenizer read the same way. There is NO pipe, redirection, chaining or
substitution in this tool (a pipe chain was considered and left out: 6 of 4706 real
calls used one). `check()` also returns the command rebuilt from the parsed words;
the caller runs that, not the raw string.

`pct exec <vmid> -- <command>` runs a command inside a CT. The words must be exactly
`pct exec <digits> --`; the inner command is validated by this same function; one
level only; no unquoted glob in it (the host's shell would expand it on the host).
"""
from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Optional

# ----------------------------------------------------------------------------- data
# Reused option sets.
_GREP = (
    "-E -F -G -P -i -y -w -x -z -s -v -V -b -n -H -h -o -q -a -I -r -R -L -l -c -T -Z -U "
    "-e: -f: -m+ -d: -D: -A+ -B+ -C+ -NUM "
    "--extended-regexp --fixed-strings --basic-regexp --perl-regexp --regexp= --file= "
    "--ignore-case --no-ignore-case --word-regexp --line-regexp --null-data --no-messages "
    "--invert-match --max-count= --byte-offset --line-number --line-buffered "
    "--with-filename --no-filename --label= --only-matching --quiet --silent "
    "--binary-files= --text --directories= --devices= --recursive --dereference-recursive "
    "--include= --exclude= --exclude-dir= --files-without-match --files-with-matches "
    "--count --initial-tab --null --before-context= --after-context= --context= "
    "--group-separator= --no-group-separator --color? --colour? --binary"
)
_HEAD_TAIL = (
    "-c+ -n+ -q -v -z -NUM --bytes= --lines= --quiet --silent --verbose --zero-terminated"
)
_LVM_REPORT = (
    "-a -o: -O: -S: -v --all --options= --sort= --select= --units= --noheadings "
    "--nosuffix --separator= --reportformat= --binary --aligned --readonly"
)
_PVE_GUEST_READ = {
    "config": "--current --snapshot=",
    "list": "",
    "status": "--verbose",
    "listsnapshot": "",
    "pending": "",
}

SPEC: dict[str, object] = {
    # --- files / text
    "cat": "-A -b -e -E -n -s -t -T -u -v --show-all --number-nonblank --show-ends "
           "--number --squeeze-blank --show-tabs --show-nonprinting",
    "tac": "-b -r -s: --before --regex --separator=",
    "nl": "-b: -n: -w: -s: -v: -i: --body-numbering= --number-format= --number-width= "
          "--number-separator= --starting-line-number= --line-increment=",
    "head": _HEAD_TAIL,
    "tail": _HEAD_TAIL,  # no -f/-F/--follow/--retry/--pid/-s: they block
    "ls": "-a -A -b -B -c -C -d -f -F -g -G -h -H -i -k -l -L -m -n -N -o -p -q -Q -r -R "
          "-s -S -t -u -U -v -x -X -Z -1 -I: -T: -w: "
          "--all --almost-all --author --escape --block-size= --ignore-backups --color? "
          "--directory --classify? --file-type --format= --full-time "
          "--group-directories-first --no-group --human-readable --si "
          "--dereference-command-line --dereference-command-line-symlink-to-dir --hide= "
          "--indicator-style= --inode --ignore= --kibibytes --dereference "
          "--numeric-uid-gid --literal --hide-control-chars --show-control-chars "
          "--quote-name --quoting-style= --reverse --recursive --size --sort= --time= "
          "--time-style= --tabsize= --width= --context --zero",
    "stat": "-L -f -t -c: --dereference --file-system --terse --cached= --format= --printf=",
    "du": "-0 -a -b -c -D -H -h -k -L -l -m -P -S -s -x -B: -d+ -t: "
          "--null --all --apparent-size --block-size= --bytes --total --dereference-args "
          "--max-depth= --summarize --human-readable --inodes --dereference --count-links "
          "--no-dereference --separate-dirs --si --threshold= --time? --time-style= "
          "--exclude= --one-file-system",
    "df": "-a -h -H -i -k -l -P -T -v -B: -t: -x: --all --block-size= --human-readable "
          "--si --inodes --local --no-sync --output? --portability --total --type= "
          "--print-type --exclude-type=",  # not --sync
    "wc": "-c -m -l -L -w --bytes --chars --lines --max-line-length --words --total=",
    "readlink": "-f -e -m -n -q -s -v -z --canonicalize --canonicalize-existing "
                "--canonicalize-missing --no-newline --quiet --silent --verbose --zero",
    "realpath": "-e -m -L -P -q -s -z --canonicalize-existing --canonicalize-missing "
                "--logical --physical --quiet --strip --no-symlinks --zero "
                "--relative-to= --relative-base=",
    "findmnt": "-m -k -s -A -f -i -R -U -a -b -C -c -D -e -I -J -l -n -P -r -u -v -y -x -H "
               "-F: -N: -d: -Q: -M: -S: -T: -t: -O: -o: "
               "--mtab --fstab --all --first-only --invert --submounts --uniq --ascii "
               "--bytes --nocanonicalize --canonicalize --df --evaluate --dfi --json --list "
               "--noheadings --options= --output= --output-all --pairs --raw --tree "
               "--notruncate --nofsroot --types= --target= --source= --mountpoint= --real "
               "--pseudo --shadowed --list-columns --verbose --verify --kernel? "
               "--tab-file= --task= --direction= --filter=",  # not -p/--poll
    "getfacl": "-a -d -c -e -E -s -R -L -P -t -n -p --access --default --omit-header "
               "--all-effective --no-effective --skip-base --recursive --logical "
               "--physical --tabular --numeric --absolute-names",
    "file": "-b -i -k -L -h -s -N -0 -r --brief --mime --mime-type --mime-encoding "
            "--keep-going --dereference --no-dereference --special-files --no-pad --print0 "
            "--raw",  # not -C/-m/-p/-f; not -z/-Z (spawn decompressors)
    "grep": _GREP,
    "egrep": _GREP,
    "zgrep": "-E -F -i -w -x -s -v -n -H -h -o -q -a -l -L -c -e: -m+ -A+ -B+ -C+",
    "zcat": "-f -q -v --force --quiet --verbose",
    # --- system / hardware
    "free": "-b -k -m -g -h -l -L -t -v -w --bytes --kilo --mega --giga --tera --peta "
            "--kibi --mebi --gibi --tebi --pebi --human --si --lohi --line --total "
            "--committed --wide",  # not -s/-c: they repeat
    "uptime": "-p -s --pretty --since",
    "date": "-u -R -d+ -f: -r: -I? --utc --universal --rfc-email --debug --iso-8601? "
            "--resolution --date= --file= --reference= --rfc-3339=",  # not -s/--set
    "hostname": "-s -f -d -i -I -A -a --short --fqdn --long --domain --ip-address "
                "--all-ip-addresses --all-fqdns --alias",  # not -F/-b/-y/NAME
    "uname": "-a -s -n -r -v -m -p -i -o --all --kernel-name --nodename --kernel-release "
             "--kernel-version --machine --processor --hardware-platform --operating-system",
    "nproc": "--all --ignore=",
    "arch": "",
    "getconf": "-a",
    "lscpu": "-a -b -B -c -J -r -x -y -C? -e? -p? --all --online --bytes --offline --json "
             "--raw --hex --physical --output-all --caches? --extended? --parse? "
             "--hierarchic?",
    "lsblk": "-A -D -J -M -O -P -S -N -v -a -b -d -f -i -l -m -n -p -r -s -t -z -T? "
             "-E: -I: -Q: -e: -o: -w: -x: "
             "--noempty --discard --dedup= --include= --json --merge --output-all --pairs "
             "--filter= --scsi --nvme --virtio --tree? --all --bytes --nodeps --exclude= "
             "--fs --ascii --list --perms --noheadings --output= --paths --raw --inverse "
             "--topology --width= --sort= --zoned",
    "lsmod": "",
    "lspci": "-m -t -v -k -n -D -P -s: -d:",  # not -x (config dumps), -q (DNS query)
    "lsusb": "-v -t -s: -d: --verbose --tree",
    "dmidecode": "-q -u -s: -t: -H: --quiet --dump --string= --type= --handle= "
                 "--list-strings --list-types --no-quirks --no-sysfs --oem-string=",
    "dmesg": "-H -J -k -P -p -r -S -u -x -d -e -T -t -L? -f: -l: -s: --human --json "
             "--kernel --color? --nopager --force-prefix --raw --noescape --syslog "
             "--userspace --decode --show-delta --reltime --ctime --notime --facility= "
             "--level= --buffer-size= --time-format= --since= --until=",
             # not -C -c -D -E -n -w -W -F -K
    # --- processes / network / users
    "ps": "-A -a -d -e -N -T -L -f -F -l -H -w -j -y -M -o: -p: -C: -u: -U: -G: -g: "
          "-q: -s: -t: -O: --sort= --no-headers --no-heading --headers --width= --cols= "
          "--columns= --ppid= --pid= --user= --deselect --forest --format=",
    "ss": "-h -V -n -r -a -l -B -o -e -m -p -T -i -s -b -Z -z -4 -6 -0 -t -M -S -u -d -w "
          "-x -H -Q -O -f: -A: -N: --numeric --resolve --all --listening "
          "--bound-inactive --options --extended --memory --processes --threads --info "
          "--summary --tos --cgroup --bpf --context --contexts --ipv4 --ipv6 --packet "
          "--tcp --mptcp --sctp --udp --dccp --raw --unix --tipc --vsock --xdp "
          "--no-header --no-queues --oneline --tipcinfo --inet-sockopt --family= "
          "--query= --socket= --net=",  # not -K -D -F -E
    "lsof": "-a -n -P -t -l -w -i? -p: -u: -c:",
    "pgrep": "-l -a -v -w -c -f -i -n -o -x -A -d: -g: -G: -O: -P: -s: -t: -u: -U: -r: "
             "--delimiter= --list-name --list-full --inverse --lightweight --count --full "
             "--pgroup= --group= --ignore-case --newest --oldest --older= --parent= "
             "--session= --terminal= --euid= --uid= --exact --runstates= "
             "--ignore-ancestors --cgroup= --ns= --nslist=",
    "pidof": "-s -x -q -o:",
    "vmstat": "-a -f -m -n -s -d -D -w -t -y -p: -S: --active --forks --slabs "
              "--one-header --stats --disk --disk-sum --wide --timestamp --no-first "
              "--partition= --unit=",
    "iostat": "-c -d -h -k -m -N -s -t -x -y -z --human --pretty",
    "mpstat": "-A -u -P: -I:",
    "numastat": "-m -n -c -v -z -p:",
    "who": "-a -b -d -H -l -m -p -q -r -s -t -T -u --all --boot --dead --heading "
           "--login --lookup --mesg --process --count --runlevel --short --time --users",
    "w": "-h -u -s -f -o -i -p --no-header --no-current --short --from --old-style "
         "--ip-addr --pids",
    "last": "-x -F -w -i -a -d -R -n: -s: -t: --limit= --system --fulltimes --fullnames "
            "--ip --hostlast --dns --nohostname --since= --until=",
    "id": "-u -g -G -n -r -z -Z --user --group --groups --name --real --zero --context",
    "groups": "",
    "whoami": "",
    "getent": "-i -s: --no-idn --service=",
    # --- LVM / ZFS / Proxmox / packages
    "pvs": _LVM_REPORT,
    "lvs": _LVM_REPORT + " --segments",
    "vgs": _LVM_REPORT,
    "arcstat": "-a -h -p -v -x -z -f: -s:",  # not -o FILE
    "arc_summary": "-a -d -g -r -s: --alternate --description --graph --raw --section=",
    "pveversion": "-v --verbose",
    "journalctl": "-r -x -a -q -l -e -m -N -k -h -V -n? -b? "
                  "-u: -t: -T: -p: -g: -S: -U: -o: -D: -M: -F: -c: -i: "
                  "--reverse --catalog --all --quiet --full --no-full --pager-end --merge "
                  "--dmesg --fields --list-boots --disk-usage --header --verify --utc "
                  "--show-cursor --system --user --no-pager --no-hostname --list-catalog "
                  "--dump-catalog --list-namespaces --help --version --boot? --lines? "
                  "--case-sensitive? --unit= --user-unit= --identifier= "
                  "--exclude-identifier= --priority= --grep= --since= --until= --output= "
                  "--output-fields= --directory= --file= --root= --machine= "
                  "--field= --cursor= --after-cursor= --facility= --namespace= "
                  "--verify-key=",
                  # not -f --follow --setup-keys --update-catalog --rotate --flush
                  # --sync --vacuum-* --relinquish-var --cursor-file --image (mounts)
    "smartctl": "-h -V -i -a -x -c -A -H -j? -l+ -d: -n: -T: -q: -g: -f: "
                "--help --version --info --all --xall --scan --scan-open --capabilities "
                "--attributes --health --json? --identify? --log= --device= --nocheck= "
                "--tolerance= --quietmode= --get= --format=",
                # not -s -o -S -t -X -C --smart --set --test --abort ...
    "dpkg-query": "-s -p -L -l -W -S -c -f: --status --print-avail --listfiles --list "
                  "--show --search --control-list --control-show --control-path "
                  "--load-avail --no-pager --showformat=",
    # --- subcommand-restricted: "" holds options valid before AND after the subcommand
    "zfs": {
        "list": "-r -H -p -d: -o: -s: -S: -t:",
        "get": "-r -H -p -d: -o: -s: -t:",
        "version": "",
        "holds": "-r -H -p",
    },
    "zpool": {
        "status": "-x -v -P -g -L -p -t -D -e -i -s -T:",   # not -c: runs zpool.d scripts
        "list": "-H -g -L -p -P -v -o: -T:",
        "get": "-H -p -o:",
        "history": "-i -l",
        "iostat": "-v -l -y -g -L -P -p -q -r -w -H -n -T:",  # not -c
        "version": "",
    },
    "systemctl": {
        "": "-l -a -q -r -t: -p: -P: -n: -o: --no-pager --no-legend --plain --full --all "
            "--user --system --failed --quiet --value --recursive --reverse --after "
            "--before --with-dependencies --show-types --type= --state= --property= "
            "--lines= --output= --timestamp=",
        **{v: "" for v in ("status", "show", "is-active", "is-enabled", "is-failed",
                           "list-units", "list-timers", "list-unit-files", "cat",
                           "list-jobs", "list-dependencies", "list-sockets",
                           "get-default")},
    },
    "pct": {**_PVE_GUEST_READ, "df": "", "exec": ""},
    "qm": {**_PVE_GUEST_READ, "list": "--full", "showcmd": "--pretty --snapshot="},
    "pvesm": {
        "status": "--content= --storage= --target= --enabled",
        "list": "--content= --vmid=",
        "path": "",
    },
    "pvesh": {"get": "--output-format= --noborder --noheader --human-readable --quiet "
                     "--type= --source= --typefilter= --limit= --start= --errors "
                     "--userfilter= --vmid= --since= --until= --full --content="},
    "apt": {"list": "-a --upgradable --installed --all-versions --manual-installed"},
    "apt-cache": {
        "": "-q -i -a -f -n --quiet --important --full --all-versions --no-all-versions "
            "--all-names --recurse --installed --names-only --no-pre-depends "
            "--no-depends --no-recommends --no-suggests --no-conflicts --no-breaks "
            "--no-replaces --no-enhances",  # not -p -s -o -c: they name/write cache files
        **{v: "" for v in ("show", "showpkg", "showsrc", "stats", "unmet", "search",
                           "depends", "rdepends", "pkgnames", "policy", "madison")},
    },
    "apt-mark": {v: "" for v in ("showhold", "showmanual", "showauto", "showinstall")},
    "chronyc": {
        "": "-n -c -4 -6",
        "tracking": "", "sources": "-a -v", "sourcestats": "-a -v", "activity": "",
        "ntpdata": "", "serverstats": "", "selectdata": "-a -v", "clients": "",
    },
    "coredumpctl": {
        "": "-r -q -1 -S: -U: -n: --no-pager --no-legend --reverse --quiet --since= "
            "--until= --json=",
        "list": "", "info": "",   # not dump/debug, not -o/--output
    },
    "proxmox-boot-tool": {"status": ""},
    "ip": "special",   # _ip(): [flags] OBJECT [show|list|…]
}

# Operand rules, by program or "program subcommand" (default: any operand).
# "none": no operand at all. "count": a trailing `INTERVAL` repeats forever, so the
# numbers must be exactly `INTERVAL COUNT` at the end, COUNT a whole number > 0
# (`vmstat 1 5`); every other operand is a name (starts with a letter or `/`), so no
# `+1`, ` 1`, `.5`, `0_0`, `0x1` a number parser might read, and no glob.
OPERANDS = {
    "date": "+",          # only +FORMAT; a bare operand sets the clock
    "tail": "+N",         # `+N` only: `tail +1f FILE` is the obsolete form of -f
    "lsof": "no+",        # `+r` repeats forever, `+m` writes a mount supplement file
    "hostname": "none", "uname": "none", "free": "none", "uptime": "none",
    "nproc": "none", "arch": "none", "lsmod": "none", "whoami": "none",
    **{k: "count" for k in ("vmstat", "iostat", "mpstat", "arcstat", "zpool status",
                            "zpool list", "zpool iostat")},
}
# Value allow-lists: (program, option) -> fullmatch pattern for the value.
_SMART_LOG = (r"error|xerror(,\d+)?|selftest|xselftest(,\d+)?|selective|directory|"
              r"background|scttemp|scttempsts|scttemphist|sasphy|sataphy|devstat(,\d+)?|"
              r"defects|ssd")   # not scterc,… / scttempint,… (they set)
VALUES = {("smartctl", "-l"): _SMART_LOG, ("smartctl", "--log"): _SMART_LOG}
# Optional value given as the NEXT word, when that word matches (systemd's own rule).
_LINES = r"[-+]?\d+|all"
_BOOT = r"[-+]?\d+|all|[0-9a-fA-F]{32}([-+]\d+)?"
NEXT = {("journalctl", "-n"): _LINES, ("journalctl", "--lines"): _LINES,
        ("journalctl", "-b"): _BOOT, ("journalctl", "--boot"): _BOOT,
        ("lsof", "-i"): r"[^-+].*"}

# `ip [OPTIONS] OBJECT [ACTION [selectors]]` — only reading actions; a missing action
# is a show; `get` is a lookup for route/neigh. Exact words only (`ip link s` = set).
IP_FLAGS = {"-s", "-stats", "-statistics", "-d", "-details", "-br", "-brief", "-c",
            "-color", "-4", "-6", "-0", "-j", "-json", "-p", "-pretty", "-o", "-oneline",
            "-h", "-human", "-human-readable", "-a", "-all", "-t", "-timestamp", "-r",
            "-resolve", "-iec"}   # not -b/-batch/-force/-n/-netns
IP_VALUE_OPTS = {"-f", "-family"}
IP_OBJECTS = {"address", "addr", "a", "link", "l", "route", "r", "ro", "neighbour",
              "neighbor", "neigh", "n", "rule", "ru", "maddress", "maddr", "m",
              "netconf", "netns"}
IP_READ = {"show", "list", "lst", "ls"}
IP_GET = {"route", "r", "ro", "neighbour", "neighbor", "neigh", "n"}

_PATH_DIRS = {"/usr/bin", "/usr/sbin", "/bin", "/sbin"}
_PCT_EXEC_MAX_DEPTH = 1

# ----------------------------------------------------------------------- tokenizer
_UNQUOTED_BAD = set(";&|<>()$`\\{}!")
_DQUOTED_BAD = set("$`\\")
_GLOB = set("*?[")


def _tokenize(command: str):
    """Split like bash for the restricted grammar above. Returns (words, error);
    each word is a list of (text, quoted) segments."""
    bad = sorted({c for c in command if ord(c) < 32 and c != "\t" or ord(c) == 127})
    if bad:
        return None, "control character(s) " + " ".join(repr(c) for c in bad)
    words, cur, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t":
            if cur is not None:
                words.append(cur)
                cur = None
            i += 1
            continue
        if c in "'\"":
            end = command.find(c, i + 1)
            if end < 0:
                return None, "unterminated quote"
            text = command[i + 1:end]
            if c == '"' and _DQUOTED_BAD.intersection(text):
                return None, "'$', '`' or '\\' inside double quotes"
            cur = (cur or []) + [(text, True)]
            i = end + 1
            continue
        j = i
        while j < n and command[j] not in " \t'\"":
            j += 1
        text = command[i:j]
        meta = sorted(_UNQUOTED_BAD.intersection(text))
        if meta:
            return None, "shell character(s) " + " ".join(meta)
        if cur is None and text[0] == "#":
            return None, "a comment"
        # Keeps every expansion of an operand glob an operand (see check()).
        g = next((p for p, ch in enumerate(text) if ch in _GLOB), None)
        if g is not None and not "".join(t for t, _ in cur or []) + text[:g]:
            return None, f"a word starting with a glob ({text!r}: it could expand to an option)"
        # Tilde expansion: at the start of a word, and after `=`/`:` in `NAME=...`
        # (refused after any `=`/`:`). Inside `pct exec` it would name the host's home.
        before = "".join(t for t, _ in cur or [])
        if any((before + text[:p])[-1:] in ("", "=", ":")
               for p, ch in enumerate(text) if ch == "~"):
            return None, f"an unquoted '~' in {text!r} (bash expands it; quote it or write the path)"
        cur = (cur or []) + [(text, False)]
        i = j
    if cur is not None:
        words.append(cur)
    return words, None


def _rebuild(words) -> str:
    return " ".join("".join(t if not q else shlex.quote(t) for t, q in w) or "''"
                    for w in words)


# ----------------------------------------------------------------------- validator
_parsed: dict[str, tuple] = {}


def _parse_spec(spec: str) -> tuple:
    if spec not in _parsed:
        flags, vals, attach, opt, lflags, lvals, lopt = (set() for _ in range(7))
        num = False
        for tok in spec.split():
            if tok == "-NUM":
                num = True
            elif tok.startswith("--"):
                name, kind = (tok[:-1], tok[-1]) if tok[-1] in "=?" else (tok, "")
                {"": lflags, "=": lvals, "?": lopt}[kind].add(name)
            else:
                ch, kind = tok[1], tok[2:]
                {"": flags, ":": vals, "+": vals, "?": opt}[kind].add(ch)
                if kind == "+":
                    attach.add(ch)
        _parsed[spec] = (flags, vals, attach, opt, lflags, lvals, lopt, num)
    return _parsed[spec]


def _options(prog: str, spec: str, args: list[str],
             stop_at_operand: bool = False) -> tuple[Optional[str], list[int], int]:
    """Walk `args` against `spec`. Returns (error, operand indices, index_of_stop)."""
    flags, vals, attach, opt, lflags, lvals, lopt, num = _parse_spec(spec)
    operands: list[int] = []
    k = 0

    def no(word: str, why: str = "is not a listed read-only option") -> str:
        return f"'{prog} {word}' {why}."

    def value_ok(name: str, value: str) -> Optional[str]:
        rx = VALUES.get((prog, name))
        if rx is not None and not re.fullmatch(rx, value):
            return no(f"{name} {value}", "is not an allowed value")
        return None

    def next_value(name: str) -> Optional[str]:
        """An optional value as the NEXT word takes it when it matches NEXT; a glob
        there is refused, as its expansion could match and be taken (`-n 1*`)."""
        nonlocal k
        rx = NEXT.get((prog, name))
        if rx and k < len(args):
            if re.fullmatch(rx, args[k]):
                k += 1
            elif _GLOB.intersection(args[k]):
                return no(f"{name} {args[k]}", "is ambiguous: the glob could expand into "
                          f"{name}'s value; give the value first (`{name} 50 {args[k]}`)")
        return None

    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            if stop_at_operand:   # the next word is the subcommand
                return None, operands, k
            operands += range(k, len(args))
            return None, operands, len(args)
        if not a.startswith("-") or a == "-":
            if stop_at_operand:
                return None, operands, k - 1
            operands.append(k - 1)
            continue
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in lflags and not eq:
                continue
            if name in lopt:
                err = None if eq else next_value(name)
                if err:
                    return err, [], k
                continue
            if name in lvals:
                if not eq:
                    if k >= len(args):
                        return no(a, "needs a value"), [], k
                    val = args[k]
                    k += 1
                err = value_ok(name, val)
                if err:
                    return err, [], k
                continue
            return no(a), [], k
        if num and a[1:].isdigit():
            continue
        for p, ch in enumerate(a[1:], 2):
            if ch in flags:
                continue
            if ch in vals:
                if p < len(a):
                    if ch not in attach:
                        return no(a, f"attaches a value to -{ch} (write -{ch} VALUE)"), [], k
                    val = a[p:]
                else:
                    if k >= len(args):
                        return no(a, "needs a value"), [], k
                    val = args[k]
                    k += 1
                err = value_ok("-" + ch, val)
                if err:
                    return err, [], k
                break
            if ch in opt:
                err = next_value("-" + ch) if p == len(a) else None
                if err:
                    return err, [], k
                break
            return no(a if len(a) == 2 else f"{a} (-{ch})"), [], k
    return None, operands, len(args)


def _ip(args: list[str]) -> Optional[str]:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        k += 1
        if a in IP_VALUE_OPTS:
            k += 1
        elif a not in IP_FLAGS and a.split("=", 1)[0] not in ("-c", "-color"):
            return f"'ip {a}' is not a listed read-only ip option."
    obj = args[k] if k < len(args) else None
    if obj not in IP_OBJECTS:
        return (f"'ip {obj or ''}' is not a readable ip object (allowed: "
                + ", ".join(sorted(IP_OBJECTS)) + ").")
    action = args[k + 1] if k + 1 < len(args) else "show"
    if action in IP_READ or (action == "get" and obj in IP_GET):
        return None
    return (f"'ip {obj} {action}' is not read-only (allowed: "
            + "/".join(sorted(IP_READ)) + ", and get for route/neigh).")


def _validate(argv: list[str], depth: int) -> tuple[Optional[str], list[int]]:
    """(error, indices into argv of the free operands)."""
    head = argv[0]
    prog = PurePosixPath(head).name
    if head != prog and str(PurePosixPath(head).parent) not in _PATH_DIRS:
        return f"'{head}': a program is named bare or from {', '.join(sorted(_PATH_DIRS))}.", []
    spec = SPEC.get(prog)
    if spec is None:
        return (f"'{prog}' is not in the read-only allow-list (allowed: "
                + ", ".join(sorted(SPEC)) + ")."), []
    args = argv[1:]
    if prog == "ip":
        return _ip(args), []
    base, key = 1, prog   # argv index of the first word the final _options walks; OPERANDS key
    if isinstance(spec, dict):
        glob = spec.get("", "")
        err, _, at = _options(prog, glob, args, stop_at_operand=True)
        if err:
            return err, []
        if at >= len(args):
            if prog != "systemctl":   # a bare `systemctl [opts]` is list-units
                return f"'{prog}' needs a subcommand (allowed: {_subs(spec)}).", []
            sub, rest = "list-units", []
        else:
            sub, rest = args[at], args[at + 1:]
        if sub not in spec or sub == "":
            return f"'{prog} {sub}' is not a read-only subcommand (allowed: {_subs(spec)}).", []
        if prog == "pct" and sub == "exec":
            return _pct_exec(args[at:], depth), []
        base, key = at + 2, f"{prog} {sub}"
        err, ops, _ = _options(prog, (glob + " " + spec[sub]).strip(), rest)
    else:
        err, ops, _ = _options(prog, str(spec), args)
    if err:
        return err, []
    operands = [argv[base + i] for i in ops]
    rule = OPERANDS.get(key)
    if rule == "none" and operands:
        return f"'{prog} {operands[0]}': {prog} takes no operand here.", []
    if rule == "+" and any(not o.startswith("+") for o in operands):
        return f"'{prog}' takes only a +FORMAT operand (a bare operand sets the clock).", []
    if rule == "+N" and any(o[:1] == "+" and not re.fullmatch(r"\+\d+", o) for o in operands):
        return f"'{prog}' takes a +N operand as digits only (`+1f` would follow the file).", []
    if rule == "no+" and any(o[:1] == "+" for o in operands):
        return f"'{prog} +…' options are not listed (`+r` repeats, `+m` writes a file).", []
    if rule == "count":
        if any(_GLOB.intersection(o) for o in operands):
            return f"'{key}': no glob in its operands (one could expand into an interval).", []
        odd = [o for o in operands if not (o[:1].isalpha() or o[:1] == "/"
                                           or re.fullmatch(r"\d+(\.\d+)?", o))]
        if odd:
            return (f"'{key} {odd[0]}': an operand is a name or a plain number "
                    f"(`{key} 1 5`)."), []
        nums = [o for o in operands if o[:1].isdigit()]
        if nums and (len(nums) != 2 or operands[-2:] != nums
                     or not re.fullmatch(r"[1-9]\d*", nums[1])):
            return (f"'{key} {' '.join(nums)}': numbers must be `INTERVAL COUNT` at the end "
                    f"with a whole COUNT > 0, else it repeats until the timeout "
                    f"(`{key} {nums[0]} 5`)."), []
    return None, [base + i for i in ops]


def _subs(spec: dict) -> str:
    return ", ".join(sorted(s for s in spec if s))


def _pct_exec(args: list[str], depth: int) -> Optional[str]:
    """args = ['exec', vmid, '--', cmd...]"""
    if depth >= _PCT_EXEC_MAX_DEPTH:
        return "nested 'pct exec' is not allowed."
    if len(args) < 3 or not args[1].isdigit() or args[2] != "--" or len(args) < 4:
        return ("'pct exec' must be exactly `pct exec <vmid> -- <read-only command>` "
                "(no options, numeric vmid, '--' required, a command after it).")
    err, _ = _validate(args[3:], depth + 1)
    return None if err is None else f"inside CT {args[1]}: {err}"


KILL_AFTER = 5   # seconds between timeout's SIGTERM and SIGKILL


def check(command, timeout: Optional[float] = None) -> tuple[Optional[str], Optional[str]]:
    """(None, command_to_run) if `command` is an allow-listed read, else (reason, None).
    The command to run is rebuilt from the parsed words (see the module docstring).
    With `timeout`, it runs under coreutils `timeout -k KILL_AFTER <timeout>`, and so
    does a `pct exec` inner command (killing pct on the host need not reach the CT):
    the one bound on what the rules cannot see -- a big interval, a blocking device."""
    if not isinstance(command, str):
        return "not a string", None
    words, err = _tokenize(command)
    if err:
        return f"not a single plain command: {err}.", None
    if not words:
        return "empty command.", None
    argv = ["".join(t for t, _ in w) for w in words]
    err, operands = _validate(argv, 0)
    if err:
        return err, None
    globbed = [argv[i] for i, w in enumerate(words) if i not in operands
               and any(not q and _GLOB.intersection(t) for t, q in w)]
    if globbed:
        return (f"unquoted glob in {globbed[0]!r}: only a path operand of the host command "
                "may glob (elsewhere bash can expand it into extra words; inside `pct exec` "
                "it expands on the host). Quote it to pass it literally."), None
    if timeout is not None:
        limit = [[(w, False)] for w in ("timeout", "-k", str(KILL_AFTER), f"{timeout:g}")]
        if PurePosixPath(argv[0]).name == "pct":   # no option before its subcommand
            sub = 2 if argv[1] == "--" else 1
            if argv[sub] == "exec":
                words = words[:sub + 3] + limit + words[sub + 3:]
        words = limit + words
    return None, _rebuild(words)


def validate(command) -> Optional[str]:
    """None if `command` is an allow-listed read, else the reason."""
    return check(command)[0]
