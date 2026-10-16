# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.9.2] - 2026-10-16

### Security
- `proxmox_host_read_exec` no longer runs a command that never ends. `tail`
  accepted any operand, and GNU tail reads `tail +1f FILE` (also `+f`) as the
  obsolete form of `-f`: it followed the file until the tool's timeout, and on
  a quiet file the remote process could outlive the SSH channel. A `+N` operand
  to `tail` is now digits only. `vmstat`, `iostat`, `mpstat`, `arcstat` and
  `zpool status/list/iostat` repeat forever when given an interval alone
  (`vmstat 1`, `zpool iostat -v 1`, and a zero count for zpool): a trailing
  interval now needs a nonzero count (`vmstat 1 5`), and those operands may
  not glob (an expansion could produce a bare interval). Same rule inside
  `pct exec`.
- Real-use cost (same corpus as 1.9.1, 4973 calls): none of the 4694 calls
  1.9.1 passes is refused.
- `read_allow.py` shared byte for byte with homelab-agent-sys (0.124.3);
  pinned sha256 `28b38605…5d85`.

## [1.9.1] - 2026-10-16

### Security
- `proxmox_host_read_exec`: an **unquoted glob** (`*` `?` `[`) is now allowed only
  in a free operand of the host command. 1.9.0 inspected neither a value consumed
  by an option nor a glob in mid-word, and ran the word unquoted:
  `systemctl -p s* status pve-cluster` passed, and with files `sa` and `stop` in
  root's home bash ran `systemctl -p sa stop status pve-cluster` -- a service stop
  through the read tool. The same held for every value-taking option before a
  subcommand (`systemctl -t s* list-units`, `coredumpctl -S s* list`, `ip -f s*`).
  Refused now: a glob in an option value (`--include=*.sh`, `-e ^[[]`), in the
  program, a subcommand or any word before one, in `ip`, and anywhere in a
  `pct exec` command (it would expand on the host, not in the CT). Operands keep
  their glob (`ls /etc/s*`, `grep … /etc/pve/lxc/*.conf`): their expansions are
  more operands, never options, because no word may start with a glob. The
  error says to quote the word, which passes it literally.
- Rejected rather than quoted in the rebuilt command: `read_allow.validate()` is
  also homelab-agent-sys's S1/S5 decision on the RAW string, so it must stay true
  that an accepted string is safe as written.
- Real-use cost (audit logs of AHMETPC, CT 207, CT 215 + transcripts, 4973 calls):
  of the 4707 calls 1.9.0 passed, 13 (8 distinct) are refused -- all
  `--include=*.x`, `-e ^[[]` or a glob inside `pct exec`, each fixed by quoting.
- `read_allow.py` is shared byte for byte with homelab-agent-sys (0.123.1);
  pinned sha256 `3b051b4e…4197` in both repos.

## [1.9.0] - 2026-10-16

### Security
- `proxmox_host_read_exec` is now a pure **allow-list** (madde 309). The
  deny-list scanner it replaced was beaten in every review round (madde 295,
  310, 312, 316, 317): GNU prefixes (`dmidecode --dump-b`, `dmesg --cle`,
  `coredumpctl --outp`), an attached `-oKEY=VAL` (`apt-cache -oDir::Cache=…`),
  an unlisted smartctl log that writes (`-l scttempint,N`), a value-taking
  option hiding the subcommand. Now nothing passes unless it is listed:
  - **program** by bare name (or from `/usr/bin`, `/usr/sbin`, `/bin`,
    `/sbin`); **subcommand** for zfs, zpool, systemctl, pct, qm, pvesm, pvesh,
    apt, apt-cache, apt-mark, chronyc, coredumpctl, proxmox-boot-tool, and
    `ip OBJECT show/list` (route/neigh `get`);
  - **option** by exact spelling, short and long forms listed separately — no
    abbreviation; `--opt=val` only for a value-taking option; an attached
    short value (`-n50`, `-A3`, `-d1`) only where listed; values of
    `smartctl -l` from a list; `date` takes only `+FORMAT`, `hostname` no operand;
  - `pct exec <vmid> -- <cmd>`: exactly that form, one level, the inner command
    checked by the same list.
- Shell: a quote-aware tokenizer replaces "refuse any metacharacter in the raw
  string". Refused: these characters unquoted — `;` `&` `|` `<` `>` `(` `)` `$`
  backtick `\` `{` `}` `!`; a leading `#` or glob; `$`, backtick or `\` inside
  double quotes; control characters. Quoted text may
  hold anything (`grep -E 'a|b'`, `journalctl -g "reboot|shutdown"` now pass).
  No pipes at all (6 of 4706 real calls used one). The tool runs the command
  **rebuilt from the parsed words**, not the raw string.
- The data and the validator live in `proxmox_mcp/read_allow.py`, kept
  byte-identical in homelab-agent-sys (its `read_exec_reddi` second layer runs
  the same file). Both repos pin its sha256 (`tests/test_host_read.py`).

### Removed
- The deny-list layers of `host_read.py` (`_READ_ALLOW`, `_DENIED_FLAGS`,
  `_FOLLOW_HANGS`, `_OPTS`, `_PRE_SUB_FLAGS`, `_check_ip`, `_check_options`) and
  the `host_ssh.is_destructive` backstop for this tool (it also refused reads
  that merely mention `reboot`/`shutdown`). `proxmox_host_exec`'s gate is
  unchanged.
- Programs with no real read-exec use that have write/state options: `tree`,
  `lshw`, `sensors`, `zdb`, `nvme`, `lsscsi`, `pvdisplay`/`lvdisplay`/`vgdisplay`.
- Also refused now: `zpool status/iostat -c` (runs zpool.d scripts), `ip -n`,
  `lspci -x`/`-q` (config dumps / DNS query), `journalctl --image=` (mounts an
  image), `file -z/-Z` (spawn decompressors), `ps` BSD `k` sort key (`--sort=` works).

### Measured
- Every real read-exec call (AHMETPC + CT 207 + CT 215 audit logs, Claude Code
  transcripts: 4706 calls, 1315 distinct) replayed: of the 4321 calls 1.8.4
  passed, 4317 still pass (99.91 %; CT 207 99.87 %). The 4 lost: `ps … k -pcpu`
  (2), `pct status N -v` (abbreviation), `grep -F [ …` (leading glob). Newly
  passing: 125 calls (96 distinct), all quoted patterns or reads the old
  backstop refused for a word.

## [1.8.4] - 2026-10-15

### Security
- `proxmox_host_read_exec` (granted to read-only agents) ran mutating forms
  of allow-listed binaries (madde 312, measured by sys-304). Examples:
  - `ip link set vmbr0 down` / `ip -s link set …` / `ip addr add` /
    `ip route flush all` / `ip neigh flush`: the "subcommand" check took the
    object (`link`) for the action;
  - `date -s`/`--set=`, `hostname evil`/`-F`;
  - `tree -o /etc/passwd`, `arcstat -o`, `lshw -dump`, `file -C`,
    `nvme telemetry-log --output-file=` (write a file);
  - `ss -K`, `smartctl -s off`/`--smart=off`, `sensors -s`;
  - `journalctl --setup-keys`/`--update-catalog`, `apt-cache gencaches`,
    `zdb -e -F`;
  - and all of them through `pct exec <vmid> -- …`.
- Now fail-closed at the option level. The binaries that can also write or
  change state (date, hostname, tree, ss, arcstat, smartctl, journalctl,
  sensors, lshw, file, apt-cache, zdb) accept only options listed as read
  forms in `_OPTS`, from their `--help`/man pages. Clusters (`-ia`), attached
  values (`-n50`, `-Iseconds`), `--opt=value` and journalctl's optional
  values (`-n 50`, `-b -1`) are parsed; unknown options and GNU
  abbreviations (`date --se`) are refused. Operand checks: `date` only
  `+FORMAT`, `hostname` none, `smartctl -l scterc,…` (sets a timer) refused.
- `ip` is parsed on its own: `ip [read options] OBJECT [show|list|ls|lst]`,
  plus `get` for route/neigh. Every other action is refused, and so are
  `-b/-batch`, `-force` and `netns exec`. Only exact words count, because
  ip takes prefixes and `ip link s` is `set`.
- Subcommand-restricted binaries refuse an option before the subcommand
  unless it is a known value-less flag or an attached `--opt=value`.
  `systemctl --state status stop pveproxy` passed: `--state` swallowed
  `status`, and `stop` ran.
- `telemetry-log` was dropped from the nvme read subcommands, and so was
  `gencaches` from apt-cache (now subcommand-restricted). nvme `-O/--output-file`
  and coredumpctl `-o/--output` are denied. `systemctl` with no verb
  (`systemctl --failed`) is `list-units` and passes.
- False positives: all 726 unique `proxmox_host_read_exec` commands from the
  Claude Code transcripts were replayed. None newly refused; 2 now pass
  (`systemctl --failed`). `test_normal_reads_still_pass` covers ~70 everyday
  read forms (`ip -br a`, `ss -tlnp`, `journalctl -u x -n 50`,
  `smartctl -a`, `date +%s`, `hostname -I`, `lshw -short`, …).
- The refusal for a bad subcommand printed `'.strip()` literally; fixed.

## [1.8.3] - 2026-10-15

### Security
- The destructive-pattern gate's scanner (1.8.2) let these through without
  `i_understand_data_loss`; 1.8.1 caught the first four (madde 310):
  - `echo $'a\'b'; eval "$X"` and `echo $'\'' ; curl x | sh`. ANSI-C
    quoting was read as a plain `'…'`, so `\'` closed it and the next quote
    swallowed the rest. `$'…'` is now decoded the way bash does (`\xHH`,
    octal, `\u`, `\cX`, `\n`…), so `$'\x65val'` is `eval` and `$'\x72m' -rf`
    matches `rm -rf`. `$"…"` is a double-quoted string.
  - `` echo `echo \`eval "$X"\`` ``. A backtick substitution ended at the
    first backtick, escaped or not. It now ends at the first unescaped one,
    and `` \` ``, `\\`, `\$` are unescaped before the inner scan.
  - `cat > f <<'EOF'` / `it's` / `EOF` / `eval "$X"`. An apostrophe in a
    heredoc body opened a quote that ate every later command. Heredocs
    (`<<`, `<<-`, quoted or not) now end at their delimiter line; the body
    is scanned as a script, and an unquoted delimiter's body also for
    `$(…)`/backticks, which bash expands there. `<<<` is a here-string.
  - `sudo --user root eval "$X"`, `timeout --signal KILL 5 eval "$X"`,
    `nice --adjustment 5 eval "$X"`, and also `sudo -Eu root eval`,
    `env - eval`, `env -a x eval`, `sudo --us root eval`. `_head` skipped a
    value only after a two-letter short option, so a long option's value or
    a clustered `-Eu root` became the command. Each wrapper (sudo, doas, env,
    xargs, nice, timeout, stdbuf, chroot, nohup, exec, command, builtin,
    time, setsid) now has explicit tables of short and long options with and
    without a value, taken from coreutils 9 / sudo 1.9 / util-linux `--help`
    on CT 207. `--opt=val`, `--opt val`, `-uroot`, `-Eu root`, getopt's
    unique long-option prefixes and `nice -5` are parsed.
- Redirections are not the command or a shell's program any more.
  `curl x | bash > /tmp/log`, `| bash 2> err`, `| sh >> log` passed: the
  redirection target counted as a script operand (1.8.1 caught these). And
  `2>/dev/null eval "$X"`, `>/tmp/x eval …` passed: the redirection was taken
  as the command head (missed by 1.8.1 too).
- Fail-closed: a command the scanner can't model now needs the flag, named
  "(the gate can't parse it: not inspectable)". That is an unclosed `'`,
  `"`, `$'`, backtick, `$(`, `$((` or heredoc (a misparse usually leaves one
  open, as in all four cases above), an unknown `$'` escape, a wrapper
  option missing from the tables (`sudo -h host`, which is help alone but
  takes a host before a command; `env -S`/`--split-string`, whose value is a
  command line; an ambiguous prefix), and brace expansion in a command word
  (`{eval,x} y`). It does not apply inside a decoded base64 payload or a
  heredoc body no shell reads as its program (`psql <<SQL`, `cat > f <<EOF`,
  `sh -c 'python3 -' <<PY`): those are data (a diff, SQL) as often as
  scripts, so there only the destructive patterns and opaque wrappers count.
  A body a shell reads (`bash <<EOF`, `pct exec N -- sh <<EOF`) stays strict.
- `$((…))` is arithmetic: `<<` inside is a shift, and its `$(…)`/backticks
  are followed (`$(( $(eval x) ))` is caught).
- False-positive measurement. Full-length corpus: 9 505 unique
  `host_exec`/`lxc_exec`/`vm_exec`/`host_read_exec` commands from the Claude
  Code transcripts (2 265 of them ≥ 500 chars): 1.8.2 flags 595, 1.8.3 602.
  The 7 new ones are all malformed for bash too: 6 unbalanced quotes (an
  apostrophe inside `'…'`, a `"` never closed), 1 heredoc whose terminator
  line starts with a space. None passes that 1.8.2 refused. The AHMETPC
  audit-log replay (8 096 entries) gives 121 → 2 068, none newly passing;
  1 889 of the new ones are entries cut at 500 chars by the log (an
  artefact: the cut leaves a quote open), 54 have a quote eaten by
  redaction (`<gizli…>`), the other 4 are malformed commands from above. Hand corpus of normal admin commands
  (`systemctl`, `journalctl --since '1 hour ago'`, `pct exec … -- sh -c '…'`,
  `sudo -u x git -C …`, `timeout --signal=TERM …`, heredocs with
  apostrophes): no refusal (`test_normal_admin_commands_still_pass`).
- Tests: every 1.8.1/1.8.2 CHANGELOG example of the gate is now a test, plus
  the regressions above and the neighbours of each fail-closed rule; the
  new gate tests fail on 1.8.2 (16/22) and pass now. 1.8.0's wrong-key
  (as opposed to keyless) `rewrite_redacted` refusal got a test too.
- Ceiling (the `kisayol:` comment in `host_ssh.py`). Closed: `$'\xNN'`,
  `env -S`, escaped nested backticks, long-option wrappers, heredocs.
  Remaining: payloads built from variables or printf, script strings handed
  to commands the scanner doesn't follow (`trap`, `parallel`), `| su user`,
  interpreter flags taken as "program given" (`| python3 -E`), `$( (…) )`
  and `case … )` closing `$(` early, a script written to a file by a heredoc
  and run later, `(( a << b ))` read as a heredoc (fails closed).

## [1.8.2] - 2026-10-15

### Security
- `eval` and a pipe into a shell are now caught at every command position,
  not only after `;`, `&`, `(`, a backtick, `{` or `then`/`do`/`else`.
  `false || eval "$X"`, `echo x | eval "$(cat)"` and `! eval "$X"` ran
  without `i_understand_data_loss` (madde 295). The opaque checks no longer
  run as regexes over the raw text. A small shell scanner splits the command
  into simple commands on unquoted `; & | || && ( ) \n` and backticks,
  strips quotes and escapes, and follows `$(…)`/`<(…)` (inside double quotes
  too), `<shell> … -c '<script>'` (also via `find -exec`, `xargs`, `su`,
  `runuser`), `ssh host <cmd>` and `pct exec`/`qm`/`lxc-attach … -- <cmd>`.
  `_head()` is the single definition of a command's head. It skips keywords
  (`if then do else elif while until ! { }`), `VAR=` assignments and wrapper
  chains with their options (`sudo -u x`, `env -i`, `xargs -0`, `timeout 5`,
  `nice -n`, `nohup`, `exec`, `command`, `time`, `setsid`, `stdbuf`,
  `chroot`). Every opaque check uses that head: `eval`, decoders, pipe to a
  shell, `sh -c "$…"` and `<(…)`.
- Newly caught: `| python3 -`, `| perl` and other interpreters reading their
  program from stdin; `| env bash` and other wrapped shells; `bash -c "$X"`;
  quote-split words (`r''m -rf`, `e'v'al`). The destructive patterns also run
  on the quote-stripped words. Nesting deeper than 20 substitutions is
  refused as not inspectable (fail-closed, no `RecursionError`).
- Narrowed (loosening, madde 291.3): `b64decode`/`uudecode` and other decode
  calls (`decodebytes`, `a2b_base64`, `decode_base64`, `decode64`) gate only
  when they are a command head or when an interpreter (`python`, `perl`,
  `ruby`, `node`, `php`) is a command head (`python3 -c "…b64decode…"`,
  `python3 - <<EOF`). A source search like `grep -rn b64decode /opt/x` now
  passes, including in the `proxmox_host_read_exec` backstop. So do
  `grep -rn "base64 -d" src` and other quoted mentions.
- Replay over the AHMETPC host and VM logs (8 035 commands, the `cmd` field
  is redacted and cut at 500 chars) gives identical decisions under 1.8.1 and
  1.8.2: 117 flagged by both, 0 newly flagged, 0 newly passing.
- Ceiling (the `kisayol:` comment in `host_ssh.py`): payloads built from
  variables or `$'\x72m'`/printf escapes, `env -S`, script strings handed to
  commands the scanner doesn't follow (`trap`, `parallel`; quote-eval remains
  as a backstop), `| su user`, interpreter flags taken as "program given"
  (`| python3 -E`).

### Changed
- `.gitignore` covers `*.kirik`, the audit log archives that 290.2 left
  untracked (`?? _host_ssh_audit.log.2026-10-15.kirik` on CT 207).

## [1.8.1] - 2026-10-15

### Security
- The destructive-pattern gate of `proxmox_host_exec`, `proxmox_lxc_exec` and
  `proxmox_vm_exec` (and the `proxmox_host_read_exec` backstop) no longer
  misses encoded or wrapped payloads. `echo <base64 of rm -rf …> | base64 -d | sh`
  ran without `i_understand_data_loss`. Base64 literals in the command are now
  decoded (nested up to three levels) and checked, so the refusal names the
  hidden pattern. Independently, wrappers whose content the check can't see
  need the flag whatever they carry (fail-closed): a decoder (`base64 -d`,
  `openssl … -d`, `xxd -r`, `b64decode`, `uudecode`), `eval` in command
  position, a pipe into a shell (`| sh`, `| sudo bash -`), `sh -c "$(…)"` and
  process substitution into a shell. Both host and guest detectors share this
  in `host_ssh.is_destructive`. Replayed over the AHMETPC host log (7 990
  commands), 14 that passed before would now need the flag: 8 `base64 -d`
  file transfers, 2 of them carrying a script with `rm -rf` / `chmod 700 /`
  (same rule as a heredoc), a `| bash -` installer, two `eval`s and a
  `. <(…)`. `/tmp/eval` paths and `grep -E 'prompt eval|eval time'` don't
  match.

### Fixed
- The audit chain no longer breaks after an entry containing U+2028, U+2029
  or U+0085. `json.dumps(ensure_ascii=False)` leaves those raw, and
  `_last_hash()` / `rewrite_redacted()` split lines with `str.splitlines()`,
  which treats them as line ends: the next append chained to `""` (verify then
  reported a prev mismatch) and a rewrite cut such a line in two. Both now split
  on `"\n"` only, and new lines escape the three characters on disk so other
  line readers can't split them either. Hashes are unchanged (taken over the
  unescaped canonical form); existing logs that hold the raw characters verify,
  chain and rewrite correctly. On the AHMETPC host log old and new code give
  identical `verify_chain`, `_last_hash` and `rewrite_redacted` results.

## [1.8.0] - 2026-10-15

### Security
- `audit.rewrite_redacted()` no longer launders a tampered line. It used to
  recompute the hash of every chained line, so a line edited after it was
  written (stored `hash` no longer matching its content) came out of a
  "redaction" rewrite verifying again; only `prev` breaks survived. A
  hash-mismatch line is now never re-signed. By default the call refuses and
  writes nothing, naming the first broken lines and pointing at the `.kirik`
  archive convention (move the log aside as `<log>.<date>.kirik`, let a fresh
  chain start). The new `force=True` rewrites the rest and only masks those
  lines, keeping their stored `hash` and `prev`, so `verify_chain` still breaks
  on the same line; the result's new `mismatched` list and the marker note
  record them.
- A keyless run over an HMAC log (`PROXMOX_AUDIT_HMAC_KEY` not loaded, or
  wrong) made every line "mismatch" and the old rewrite re-signed the whole
  log with plain SHA-256, silently stripping the HMAC. When no chained line
  verifies the call now refuses, `force` or not.

Refuse-by-default over always-mark: a mismatch can mean tampering or a wrong
key, the rewrite is irreversible, and the safe reaction differs (archive vs.
reload the key), so the operator decides. Intact logs and `prev` breaks are
unaffected: an equivalence run over legacy + chained logs, with and without
HMAC and with a deleted line, gave byte-identical output to 1.7.4.

## [1.7.4] - 2026-10-14

### Fixed
- A re-run of `audit.rewrite_redacted()` wiped the recovery copy. After an
  in-place write cut short, `<log>.redact-tmp` is the only complete rewritten
  log, but the next call opened it with `O_TRUNC` before touching the log —
  exactly what an operator re-running the tool on a truncated log would do.
  The call now refuses while that file exists. Documented trade-off of 1.7.3:
  the in-place write is not atomic for unlocked readers, so a `verify_chain`
  overlapping it may report a transient break that is gone on retry.

## [1.7.3] - 2026-10-14

### Fixed
- `audit.rewrite_redacted()` could silently lose an audit line. It took no
  lock: it compared the file size, then swapped in the rewritten file with
  `os.replace`, so a line another MCP process appended between the two was
  dropped — and the rebuilt chain stayed self-consistent, so `verify_chain`
  could not see it (reproduced; under steady appends it instead failed with
  "log kept growing"). The slow redaction pass still runs unlocked (a 6 MB log
  takes ~10 s, longer than the Windows append lock's ~10 s wait); then, under
  `append()`'s lock, lines appended meanwhile are redacted too and the file is
  rewritten **in place** through the locked fd. A locked `os.replace` would
  not do: an appender waiting on the old inode's lock would write into the
  orphaned file, and Windows refuses to replace a file another process has
  open. In place also keeps the log's owner and mode, so a root-run rewrite
  on CT 215 no longer leaves a root-owned log the agent uid can't write. The
  new content is first fsync'd to `<log>.redact-tmp`; a surviving tmp means
  the in-place write was cut short and is the complete rewritten log. New test
  races 4 writer processes against a rewrite (failed 3/3 on 1.7.2, passes now).

## [1.7.2] - 2026-10-14

### Fixed
- Concurrent MCP processes broke the audit hash chain. Every Claude client
  starts its own server (AHMETPC runs ~10), and `audit.append()` read the tail
  hash and appended without a lock, so two writers could chain to the same
  `prev` — the likely cause of the CT 207 break of 2026-10-08. Reading the tail
  and writing the line now happen under an exclusive cross-process lock on the
  log itself (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows on one byte
  at offset 2^40, so other processes' reads are never blocked). If the lock
  cannot be had within ~10 s the line is still written: a visible break beats
  a lost entry. A per-process-file design was rejected: verify/rewrite assume
  one chain per log, and CT 215's systemd sandbox opens exactly two fixed
  log paths (`ReadWritePaths=-…/_host_ssh_audit.log`), so no sibling lock or
  per-process file could be created there either. New test spawns 6 writer
  processes (failed 3/3 on 1.7.1, passes now).
- The 1.7.0 permission heal (`0644` → `0600` on append) broke group-shared
  logs: on CT 215 the sage core (uid `sage`) and its agent uid (`sage-ajan`)
  share `sage-ajan:sage 0664` logs. The owner's append would strip the group
  write bit, and a non-owner's append raised in `fchmod` and silently dropped
  the entry. The heal now strips world bits always and group bits only when
  the log is not group-writable (`0644` → `0600`, `0664` → `0660`); a failed
  `fchmod` no longer drops the line. Lines are written as raw bytes (LF on
  Windows too).

## [1.7.1] - 2026-10-13

### Fixed
- Env-literal redaction masked harmless environment values: on CT 207 one
  `LANG=en_US.UTF-8` occurrence became `<gizli:LANG>` during the 1.7.0 clean-up
  (irreversible, harmless), and on Windows `OS`, `TERM`, `PATHEXT`, `HOMEPATH`
  and `PWD` qualified. A neutral-named variable now needs a secret-like value
  (≥16 chars, letters and digits); locales, `\`-paths and `PWD`/`OLDPWD` are
  excluded. Secret-named variables and the pattern layer are unchanged.

## [1.7.0] - 2026-10-13

### Security
- **Secrets are masked in the SSH audit logs before they are written.** The
  `cmd` field and the output previews were stored verbatim: CT 207's copy held
  `PBS_TOKEN_SECRET`, `CT209_PG_HA`, `CT202_N8N_DB` and `CT201_IMMICH_JWT_SECRET`
  (all in `stdout_preview` of commands that printed other CTs' `.env`/DSNs), the
  local copy six more, and both files were `0644`. New `proxmox_mcp/redact.py`
  runs inside `audit.append()` on every string field, before the hash, so the
  chain covers the masked text:
  - *literal*: secret-looking values in the server's environment →
    `<gizli:NAME>`; chosen by value as well as name (madde 227 rules), because
    a name-only mask misses neutral names like `CT209_PG_HA`;
  - *pattern*: `NAME=value` with a secret-ish name, URL credentials, `Bearer`,
    PVE/PBS API tokens, `--password …`, PEM private keys, `sk-ant-…`/`ghp_…`/
    Telegram tokens → `<gizli>`; a base64 blob whose decoded text holds any of
    these → `<gizli:base64>`.
  `host_ssh.audit_log` / `vm_ssh.audit_log` redact before the 500/200-char cut so
  a secret is never left half-visible. Measured on both real logs: every known
  secret masked (CT 207: 4 → 0, local: 9 → 0).
- **Audit log file mode `0600`.** Created `0600`; an existing group/other-readable
  file is tightened on the next append (POSIX).

### Added
- `audit.rewrite_redacted(path, note)`: one-off clean-up of a log written before
  1.7.0 — masks every line, rebuilds the hash chain (each `prev` mapped from the
  old hash, so an already-broken link stays visible), replaces the file
  atomically `0600` and appends a marker entry. Run on CT 207 and AHMETPC on
  2026-10-13 (madde 241).

## [1.6.0] - 2026-10-08

### Security
- **PVE-registered snapshots can no longer be destroyed with raw `zfs destroy`.**
  On 2026-10-05 a `proxmox_host_exec` loop (`for s in subvol-…@a,b …; do zfs
  destroy $P/$s; done`, with `i_understand_data_loss=true`) removed 18
  snapshots from ZFS that were still `[sections]` in the guest configs; the
  follow-up `pct delsnapshot` left guests in `lock: snapshot-delete`, and CT 200
  (Postgres) and CT 202 did not start after the 2026-10-08 reboot (~1.5 h DB
  outage; incident record in homelab-project `06-known-issues.md`). The rule
  "delete PVE snapshots only with `pct/qm delsnapshot`" was written down since
  2026-07-30 but nothing enforced it. New `proxmox_mcp/pve_snapshots.py` reads
  every guest config's section headers (one SSH call) and the three ZFS-destroy
  paths refuse any match, pointing to `pct/qm delsnapshot <vmid> <snap>`:
  - `proxmox_zfs_destroy_snapshots_by_pattern` — exact: checks every match and
    refuses the whole batch; the dry run lists them up front.
  - `proxmox_cleanup_vzdump_snapshots` — skips an `@vzdump` that still has a
    `[vzdump]` section (LXC snapshot-mode vzdump creates it through
    `PVE::LXC::Config->snapshot_create($vmid, 'vzdump')`, verified in
    `VZDump/LXC.pm` on the host), removes the rest; the dry run shows the skip.
  - `proxmox_host_exec` — when the command contains `zfs destroy`, checks every
    literal `<dataset>@<snap>[,<snap>…]` anywhere in the command (so the
    incident's loop list is caught although the destroy argument was `$P/$s`).
    A variable/pool-root dataset is checked against all guests; an `a%b` range
    on a guest with snapshots is refused whole. Known limit, marked `kisayol:`
    in the code: names generated at run time (`… | xargs zfs destroy`) are
    invisible here. Refusals are written to `_host_ssh_audit.log`.
  No override flag (the incident already carried `i_understand_data_loss`), and
  an unreadable `/etc/pve` refuses instead of running blind. Unregistered
  snapshots — sanoid `autosnap_*`, ad-hoc `zfs snapshot` — pass unchanged; no
  name is special-cased. Covered by `tests/test_pve_snapshot_gate.py` (19 cases,
  including the incident command verbatim).

## [1.5.3] - 2026-09-17

### Added
- **`pyrightconfig.json`** so the Pyright language server finds this repo's
  `.venv`. Without it every third-party import (`dotenv`, `httpx`, ...) resolved
  to `reportMissingImports`, which polluted editor/agent diagnostics; measured
  with the Pyright CLI, not assumed: 1 error without the file, 0 with it.
  `build/` is excluded too -- `findReferences` was returning each symbol twice,
  once from `proxmox_mcp/` and once from the stale `build/lib/` copy.

### Fixed
- `proxmox_mcp.__version__` still said 1.5.2; aligned with `pyproject.toml` (1.5.3).

## [1.5.2] - 2026-09-03

### Fixed
- **The `!.env.example` exception added in 1.5.1 never applied.** It carried a
  trailing `# comment`, and in `.gitignore` a `#` is only a comment at the start
  of a line -- elsewhere it is part of the pattern, so the negation matched
  nothing and `.env.*` swallowed the template too. Measured in a scratch repo,
  not assumed: `git check-ignore --no-index .env.example` now resolves to the
  `!.env.example` rule. (`.env.example` stayed committable throughout because it
  is already tracked; the bug would have bitten on a fresh clone or re-add.)

## [1.5.1] - 2026-09-03

### Fixed
- **`proxmox_mcp.__version__` reported 1.3.0 while the package shipped 1.5.0.**
  The literal is public API (it is exported in `__all__`), so every client that
  imported it was told the wrong version for five weeks (2026-07-30 - 09-03).
  Synced to the packaged version and pinned by `tests/test_version.py`, which
  fails the build if `__init__.py`, `pyproject.toml` and `CHANGELOG.md` ever
  disagree again. Reading the number from installed package metadata was tried
  and rejected: an editable install keeps the version recorded at install time,
  so it goes stale just as silently (measured - the local venv still reported
  1.3.0 after the source said 1.5.0).
- **`.gitignore` did not cover `.env` variants.** `.env.bak-wan-20260902` and
  `.env.lanmode-20260902` (left over from the 2026-09-02 LAN-mode switch) were
  untracked but **not ignored**, so a `git add -A` would have committed live
  Proxmox API tokens. `.env.*` is now ignored with `!.env.example` kept tracked;
  the leftover files were deleted (the `.bak-wan` copies were byte-identical to
  the current `.env`).

## [1.5.0] - 2026-07-30

### Added
- **Operator approval for destructive tools** (`proxmox_mcp/operator_ack.py`).
  `confirm=true` is a tool argument, so it only ever proved the *model's* intent;
  nothing forced the question to reach a human. On mcp >= 2.0 the same decision
  is now put to the operator through the client, using the resolver injection of
  spec revision 2026-07-28 (`Annotated[..., Resolve(...)]` + `Elicit`). The
  approval lands in a server-filled parameter that **never appears in the
  model-facing input schema** (verified: `properties: ['params']` on both SDK
  eras), so it cannot be supplied or forged by a tool call.
  - Wired into `proxmox_snapshot` as the reference implementation. Observed
    end-to-end: operator approves -> the API call runs; operator declines -> the
    call is refused and **no HTTP write is made**, even with `confirm=true` and
    `i_understand_data_loss=true` both set by the model.
  - Degrades to the flag gate alone — today's behaviour — on mcp 1.x or when the
    client declares no form elicitation. This is deliberate: the SDK's own path
    raises `MISSING_REQUIRED_CLIENT_CAPABILITY` there, which would have made
    every destructive tool unusable on a client without human-in-the-loop.
  - The question is only asked once every model-set flag already passes, so the
    operator is never prompted for a call a later gate would refuse anyway. The
    `delete` data-loss check moved earlier in `proxmox_snapshot` for that reason
    (same message, still before any HTTP call).
  - `tests/test_operator_ack.py`: 10 tests — schema invisibility (both eras),
    when the resolver asks vs stays silent, every negative verdict, and
    approve/decline through the tool body against an HTTP spy.

### Changed
- **mcp SDK 2.x support** (spec revision 2026-07-28). `mcp_instance.py` imports
  `MCPServer` and falls back to `FastMCP` on the 1.x maintenance line; it is the
  only module that touches the SDK's server class. All 52 tools register
  unchanged — the decorator API did not move.
  - Verified over real stdio (the exact command Claude Desktop runs):
    `protocol_version=2026-07-28`, 52 tools, live read-only API call OK, both
    flag gates intact.
- **Dependency floor raised to `mcp>=2.0,<3`.** CT 207's autodeploy runs
  `pip install -e .` without `-U`, so any range that mcp 1.29.0 already satisfied
  would have left the deployed venv on the 1.x line forever. Sized as a minor
  (not major) release: the server code itself still runs on 1.x via the fallback,
  which is also what keeps it alive if that pip step ever fails.
- `serverInfo.version` is still left empty on purpose: the only single source is
  `pyproject.toml`, and reading it back from the installed dist reports a stale
  number when the server runs from a source checkout (`pve-mcp 1.3.0` installed
  against a 1.4.0 tree — exactly this trap).

## [1.4.0] - 2026-07-29

### Added
- **Read-only look INSIDE a container**: `proxmox_host_read_exec` now accepts
  `pct exec <vmid> -- <read-only command>`. Until now there was no read-only way
  to see a container from the inside at all — `proxmox_host_read_exec` covered
  only the host, and `proxmox_lxc_exec` is classified S5 (forbidden) in the
  homelab-agent-sys safety model, so an agent diagnosing "what does this service see
  from inside the CT?" had no legal tool and kept attempting the forbidden one.
- No new tool and no new safety classification: the part after `--` is
  **re-validated by the same `validate_read_command`**, so the container gets
  exactly the host allow-list. `pct exec 200 -- systemctl status postgresql`
  passes; `-- rm -rf /`, `-- systemctl restart`, `-- dmesg --clear`,
  `-- tail -f`, `-- bash -c ...` are all refused, as is a missing/non-numeric
  vmid, a missing `--`, an empty inner command, and nested `pct exec`. Rejections
  are prefixed `Refused (inside CT <vmid>):` so the cause is unambiguous. The
  shell-metacharacter gate runs before parsing, so it covers the inner command
  too, and `host_ssh.is_destructive` still sees the whole string.
- `tests/test_host_read.py`: four new test groups covering the above (15/15 pass).

## [1.3.1] - 2026-07-29

### Fixed
- **Pin `mcp` to `<2.0`.** mcp 2.x removed `mcp.server.fastmcp` (FastMCP moved
  out of the SDK), and this server opens with
  `from mcp.server.fastmcp import FastMCP`. The dependency was declared
  `mcp>=1.2.0` with no upper bound, and homelab-agent-sys's autodeploy refreshes a
  server's venv (`pip install -r` / `-e .`) whenever its manifest changes — so
  on 2026-07-29 a routine rebuild pulled 2.0.0 and **every MCP server on CT 207
  failed to start** with `ModuleNotFoundError`. The agent's whole tool layer was
  dark for ~45 minutes while its dashboard still showed green. No code change is
  needed to run on mcp 1.x; the pin just stops an unattended rebuild from
  crossing the 2.x boundary.

## [1.3.0] - 2026-07-17

### Added
- **Audit replay** — `proxmox_audit_verify` gains a `tail=N` parameter that
  lists the most recent N recorded audit entries alongside the INTACT/BROKEN
  chain verdict (backed by a new read-only `audit.read_entries()`). Verifying
  the SSH exec trail and reviewing what ran is now a single call.
- **`tests/test_safety_gates.py`** — bypass/fuzz tests proving the safety gates
  cannot be circumvented: `extra="forbid"` rejects a typo'd gate field
  (`confirmed=true`) and malformed values (`confirm="maybe"`, `confirm=2`,
  `i_understand_data_loss="yep"`); and a refused mutation (missing `confirm`,
  missing `i_understand_data_loss` on an overwrite restore, or `dry_run`) makes
  **no** HTTP write call (verified with a POST spy). Suite is now 54 tests.

### Changed
- `proxmox_audit_verify` title and description updated for the verify **+
  replay** capability; it stays read-only.

## [1.2.0] - 2026-07-17

### Added
- **Guest creation from scratch** — closes the biggest capability gap vs. other
  Proxmox MCP servers (they had create; we only had clone):
  - `proxmox_create_vm` — QEMU VM with core hardware (cores/sockets/memory/
    ostype), one virtio-scsi boot disk, one virtio NIC, and an optional install
    ISO (boots from the CD first). Requires `confirm=true`.
  - `proxmox_create_container` — LXC from a template with rootfs, one NIC (dhcp
    or a static CIDR), root access via `password` and/or `ssh_public_key`,
    unprivileged by default, optional `nesting`. Requires `confirm=true`.
  - Medium scope by design: cloud-init, multi-disk and multi-NIC are left out
    for now; add them afterwards with the config/resize tools.
- **`dry_run` preview** on the high-consequence mutations `proxmox_create_vm`,
  `proxmox_create_container`, `proxmox_clone_vm`, and `proxmox_restore_backup`.
  With `dry_run=true` the tool returns the exact endpoint + payload it would
  send (secrets masked) instead of executing. `proxmox_restore_backup` still
  runs its read-only existence probe first, so the preview reports whether the
  restore would be a fresh create or an overwrite.
- **Tamper-evident audit log** (`proxmox_mcp/audit.py`). The host- and
  guest-SSH audit logs are now hash-chained: each entry carries `prev` + `hash`
  (SHA-256, or HMAC-SHA256 when `PROXMOX_AUDIT_HMAC_KEY` is set), so altering,
  deleting (including from the head of the chain) or reordering any line is
  detectable.
  - `proxmox_audit_verify` — new read-only tool that recomputes the chain and
    reports INTACT / BROKEN with the first offending line. Pre-existing legacy
    lines (from before this release) are tolerated and don't break the chain
    that follows them.
- `tests/test_guest_create.py` (create models, dry_run previews, secret
  masking, gateway/ip validation, restore preview via a monkeypatched probe)
  and `tests/test_audit.py` (chain integrity, tamper / mid- and head-deletion
  detection, HMAC mode, wrong-key rejection).
- `pyproject.toml` — PyPI/uvx packaging with a console entry point
  (`pve-mcp` = `proxmox_mcp.server:main`), enabling one-command install via
  the `proxmox-mcp-suite` Claude Code marketplace.

### Changed
- Shared `mask_secrets()` + `dry_run_preview()` helpers added to
  `proxmox_mcp/format.py`, used by all four dry_run-capable tools.
- Tool count 49 → 52.

### Security
- Optional `PROXMOX_AUDIT_HMAC_KEY` upgrades the audit chain from
  integrity-evident (catches accidental corruption, truncation and deletion)
  to tamper-evident (an attacker who cannot read the key cannot forge a valid
  chain).

## [1.1.0] - 2026-07-04

### Added
- **`proxmox_host_read_exec` read-only allow-list greatly expanded** for host
  diagnostics (all additions are genuinely non-mutating; the safety model —
  no shell metacharacters, subcommand restriction for mutable binaries, and the
  `is_destructive` backstop — is unchanged). New pure-read binaries: LVM
  reporting (`pvs`, `lvs`, `vgs`, `pvdisplay`, `lvdisplay`, `vgdisplay`),
  hardware (`lspci`, `lsusb`, `lsscsi`, `lsmod`, `lshw`, `dmidecode`),
  process/network (`lsof`, `pgrep`, `pidof`, `vmstat`, `iostat`, `mpstat`,
  `numastat`), users (`id`, `groups`, `whoami`, `getent`, `who`, `w`, `last`),
  text/file (`grep`, `egrep`, `zgrep`, `zcat`, `getfacl`, `nl`, `tac`, `tree`),
  ZFS `zdb`, packages (`dpkg-query`, `apt-cache`), and `dmesg`. New
  subcommand-restricted: `proxmox-boot-tool status`, `nvme <read-subcmds>`,
  `apt list`, `apt-mark show*`, `chronyc <read-subcmds>`, `coredumpctl
  list/info`; extended read subcommands on `zpool`, `zfs`, `systemctl`, `qm`,
  `pct`, `pvesm`.
- **`_DENIED_FLAGS` guard** blocks the write/state escape hatches on otherwise
  read-only binaries: `dmesg -C/-c/-n/--console-*/-w` (clear ring buffer / set
  console level / follow) and `dmidecode --dump-bin/--dump` (writes SMBIOS to a
  file). Mutating LVM/nvme/apt operations stay refused (not allow-listed, or the
  wrong subcommand). Expanded `tests/test_host_read.py` (11/11 pass).

## [1.0.0] - 2026-07-03

Token-efficiency and effectiveness overhaul. **BREAKING**: 16 tools were
consolidated into 7 action-based tools (64 → 49 total); callers using the
old names must switch to the new `action=` form.

### Changed (BREAKING — tool consolidation)
- `proxmox_vm_start` / `_vm_shutdown` / `_vm_stop` / `_vm_reboot` →
  **`proxmox_vm_power`** with `action='start'|'shutdown'|'stop'|'reboot'`.
- `proxmox_create_snapshot` / `_rollback_snapshot` / `_delete_snapshot` →
  **`proxmox_snapshot`** with `action='create'|'rollback'|'delete'`
  (delete keeps the `i_understand_data_loss` gate).
- `proxmox_disk_init_gpt` / `_wipe_disk` / `_ssh_init_gpt` / `_ssh_wipe_disk`
  → **`proxmox_disk_prepare`** with `action='wipe'|'init_gpt'` and
  `via='auto'|'api'|'ssh'`; `auto` (default) tries the REST endpoint and
  transparently falls back to SSH when Proxmox rejects the API token
  ("user != root@pam") — the manual API→SSH retry dance is gone.
- `proxmox_create_lvm_vg` / `_create_lvm_thin` / `_destroy_lvm_vg` /
  `_destroy_lvm_thin` → **`proxmox_lvm_manage`** with
  `action='create_vg'|'create_thin'|'destroy_vg'|'destroy_thin'`.
- `proxmox_create_zfs_pool` / `_destroy_zfs_pool` →
  **`proxmox_zfs_pool_manage`** with `action='create'|'destroy'`.
- `proxmox_zfs_create_dataset` / `_zfs_destroy_dataset` →
  **`proxmox_zfs_dataset`** with `action='create'|'destroy'`.
- `proxmox_zfs_get_property` / `_zfs_set_property` →
  **`proxmox_zfs_property`** with `action='get'|'set'`.
- `proxmox_add_zfs_storage` / `_add_dir_storage` / `_remove_storage` →
  **`proxmox_storage_config`** with `action='add_zfs'|'add_dir'|'remove'`.
- Per-action required parameters are enforced by model validators
  (`tests/test_inputs.py`); per-action confirm / data-loss gates are
  preserved exactly.

### Added
- **`proxmox_health_overview`** — one-call compact summary (node load,
  guest states, storage fill levels with ≥85% warnings, ZFS pool health);
  the four API calls run concurrently. Replaces the usual 4–6-call
  "how is the server?" sequence.
- **`wait_seconds`** parameter (0–600, default 0) on task-launching tools:
  `proxmox_vm_power`, `proxmox_snapshot`, `proxmox_create_backup`,
  `proxmox_restore_backup`, `proxmox_move_disk`, `proxmox_clone_vm`,
  `proxmox_lvm_manage`, `proxmox_zfs_pool_manage`, `proxmox_disk_prepare`.
  The server polls the UPID task status (1s → 5s backoff) and appends the
  final `exitstatus` to the response, eliminating the follow-up status
  round trip (`http_client.wait_for_task`).
- **List filters** — `proxmox_list_vms(node=, vmid=, status=, guest_type=)`,
  `proxmox_list_backups(vmid=, limit=)` (now sorted newest-first with a
  "showing N of M" note).
- **`fields` projection** — on JSON-capable read tools
  (`fields=['vmid','name','status']` keeps only those keys per object;
  `format.project_fields`). Available on nodes/VM/storage/disk/LVM/ZFS
  listing tools and `proxmox_list_backup_jobs`.
- 30s TTL cache for VM-type resolution (`/cluster/resources` lookup) used
  by `proxmox_vm_power` and `proxmox_get_vm_status` — one fetch now serves
  a burst of related calls.
- `tests/test_inputs.py` — validation tests for the consolidated
  action-based input models.

### Changed
- `compact_json` now truncates oversized **lists at the item level** and
  appends a `{"_truncated_items": N}` marker, so truncated output stays
  valid JSON (previously a hard character cut produced invalid JSON).
  Only a single non-list payload that alone exceeds the limit still falls
  back to the hard string cap.
- Docstring diet across all tools: removed `Returns:` boilerplate from
  tool descriptions (schema tokens the model doesn't need); behavioral
  notes (confirm gates, side effects) kept.

### Removed
- `proxmox_mcp/tools/ssh_disks.py` (merged into `disks_prepare.py`).

## [0.9.1] - 2026-07-02

### Fixed
- Read-only listing tools that take no required arguments
  (`proxmox_list_nodes`, `proxmox_list_vms`, `proxmox_list_backup_jobs`,
  `proxmox_list_cluster_storage`) could not be called without arguments: they
  wrapped an all-optional Pydantic model in a single `params` parameter, and
  FastMCP marks such a parameter as `required` in the tool's JSON schema. A
  caller invoking them with no arguments got a `params Field required`
  validation error; only an explicit `params: {}` worked. Gave `params` a
  default (`FormatInput = FormatInput()`) on those four tools so `params` drops
  out of the schema's `required` list and the tools are callable with no args
  (defaulting to markdown output). Tools with genuinely required fields (e.g.
  `proxmox_get_node_status`) are unchanged and still require `params`.

## [0.9.0] - 2026-07-02

### Added
- `proxmox_host_read_exec` — an allow-listed, **read-only** counterpart to
  `proxmox_host_exec`. Runs a single non-mutating host command (cat, tail,
  journalctl, `zpool status`, `systemctl status`, `pct`/`qm` config, …) with no
  `confirm` required, so it can safely be granted to read-only agents for host
  diagnostics. Enforced in `proxmox_mcp/tools/host_read.py` by four layers:
  shell-metacharacter rejection (no pipes / redirects / substitution /
  chaining), a binary + read-subcommand allow-list, mutating-flag rejection
  (`tail -f`, `journalctl --rotate/--vacuum*`, `smartctl -t`), and the shared
  `host_ssh.is_destructive` detector as a final backstop.
- `tests/test_host_read.py` — allow/deny tests for the command validator (safe
  reads, metacharacter escapes, non-allow-listed binaries, mutating
  subcommands, hanging/state-changing flags).

### Notes
- `proxmox_host_exec` (arbitrary shell, `confirm` required) is unchanged and
  remains the escape hatch for anything outside the read-only allow-list.

## [0.8.0] - 2026-07-01

### Added
- `compact_json()` and `truncate()` helpers in `proxmox_mcp/format.py`. They
  serialize to compact JSON (no indent whitespace) and hard-cap response length
  (default 8000 chars) so a pathologically large payload can't flood the LLM
  context.
- `tests/` directory with char-budget tests for the new helpers. Runnable with
  pytest (`python -m pytest tests/`) or standalone (`python tests/test_format.py`).

### Changed
- All opt-in `response_format=json` branches across the tool modules (20 call
  sites in 14 files) now return `compact_json(...)` instead of
  `json.dumps(..., indent=2)`. This cuts ~30-40% of the bytes on the JSON path
  and adds a length cap to the only previously-unbounded output. The default
  Markdown output is unchanged.
- Removed the now-unused `import json` from the tool modules that only used it
  for the replaced dumps.

### Notes
- This is the first tracked release. Earlier development (Phases 1-7, 63 tools)
  predates this changelog and is not enumerated here; see the git history.
