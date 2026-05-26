# Proxmox MCP Sunucusu

Claude Desktop'ın (veya MCP destekli herhangi bir istemcinin) bir **Proxmox VE**
kümesini baştan sona yönetmesini sağlayan yerel bir
[MCP (Model Context Protocol)](https://modelcontextprotocol.io/) sunucusu —
VM listelemekten ZFS havuzu oluşturmaya, snapshot replikasyonuna ve host ya da
misafir VM üzerinde ad-hoc komut çalıştırmaya kadar.

Tek-node bir kurulumda **Proxmox VE 9.1.9** ile test edilmiştir. Çok-node
kümelerde de çalışır — her araç bir `node` parametresi alır.

> 🇬🇧 English README: [README.md](./README.md)

## İçeriği

**5 fazda 52 araç**, küçük bir Python paketi (`proxmox_mcp/`) içinde, her konu
ayırı modülde. Varsayılan ulaşım token tabanlı REST; SSH isteğe bağlı ve
yalnızca gerektiği durumlarda devreye girer.

### Faz 0 — küme, VM, snapshot, yedek (16 araç)

| Araç | Amaç |
|---|---|
| `proxmox_list_nodes` / `proxmox_get_node_status` | Küme node'ları, CPU/RAM/uptime |
| `proxmox_list_vms` / `proxmox_get_vm_status` | VM ve LXC envanteri + detay |
| `proxmox_vm_start` / `vm_shutdown` / `vm_stop` / `vm_reboot` | Güç işlemleri |
| `proxmox_resize_vm` | RAM / CPU yeniden boyutlandırma |
| `proxmox_list_snapshots` / `create_snapshot` / `rollback_snapshot` / `delete_snapshot` | Snapshot yaşam döngüsü |
| `proxmox_list_backups` / `proxmox_create_backup` | Yedekler (vzdump) |
| `proxmox_list_storage` | Node bazında storage kullanımı |

### Faz 1 — salt-okunur envanter (6 araç)

| Araç | Amaç |
|---|---|
| `proxmox_list_disks` / `proxmox_get_disk_smart` | Blok cihazlar + SMART (HDD/SSD/NVMe) |
| `proxmox_list_lvm` / `proxmox_list_lvm_thin` | VG, PV, LV, thin pool'lar |
| `proxmox_list_zfs` / `proxmox_get_zfs_pool` | ZFS havuzları, vdev ağacı + hata sayacı |

### Faz 2 — disk hazırlık + pool yaşam döngüsü + cluster storage (12 araç)

| Araç | Amaç |
|---|---|
| `proxmox_disk_init_gpt` / `proxmox_wipe_disk` | GPT init / wipefs (REST) |
| `proxmox_create_lvm_vg` / `proxmox_destroy_lvm_vg` | LVM VG yaşam döngüsü |
| `proxmox_create_lvm_thin` / `proxmox_destroy_lvm_thin` | LVM-thin pool yaşam döngüsü |
| `proxmox_create_zfs_pool` / `proxmox_destroy_zfs_pool` | ZFS pool yaşam döngüsü |
| `proxmox_list_cluster_storage` | `/etc/pve/storage.cfg` görünümü |
| `proxmox_add_zfs_storage` / `add_dir_storage` / `remove_storage` | Storage entry yönetimi |

### Faz 2.5 — SSH-tabanlı dataset / property / snapshot (7 araç)

Proxmox REST `wipedisk`/`initgpt`'i API token ile reddediyor ve `zfs create`/
`destroy`/`set`/`snapshot`'ı keyfi dataset'ler için expose etmiyor. Bu araçlar
allow-listli bir SSH istemcisi üzerinden bu boşluğu doldurur.

| Araç | Amaç |
|---|---|
| `proxmox_ssh_wipe_disk` / `proxmox_ssh_init_gpt` | SSH-tabanlı wipe / GPT init |
| `proxmox_zfs_create_dataset` / `proxmox_zfs_destroy_dataset` | Dataset CRUD |
| `proxmox_zfs_set_property` | Allow-listli property set (compression, atime, recordsize, …) |
| `proxmox_zfs_create_snapshot` | `zfs snapshot [-r] ds@name` |
| `proxmox_zfs_list_datasets` | Opsiyonel pool scope / snapshot dahil etme |

### Faz 3 — VM disk + ZFS read / scrub / send (7 araç)

| Araç | Amaç |
|---|---|
| `proxmox_move_disk` | Canlı disk migrasyonu QEMU / LXC |
| `proxmox_clone_vm` | Linked veya full clone, snapshot'tan opsiyonel |
| `proxmox_list_isos` | ISO envanteri |
| `proxmox_zfs_get_property` | Tek property veya hepsi |
| `proxmox_zfs_pool_status` | `zpool status [-v]` + sağlık satırı |
| `proxmox_zfs_scrub` | Scrub başlat / durdur |
| `proxmox_zfs_send` | `zfs send → dosya` veya `zfs send \| zfs recv` (replikasyon, raw, incremental) |

### Faz 4 — misafir VM shell exec (3 araç)

| Araç | Amaç |
|---|---|
| `proxmox_vm_list_hosts` | `vm_ssh_hosts.json`'daki alias'ları göster |
| `proxmox_vm_exec` | Kayıtlı VM alias'ı üzerinde tam shell komutu (audit-loglu) |
| `proxmox_vm_read_file` | Config/log dosyaları için `head -c` wrapper'ı |

### Faz 5 — Proxmox host shell exec (1 araç)

| Araç | Amaç |
|---|---|
| `proxmox_host_exec` | Proxmox host üzerinde tam shell komutu (audit-loglu) |

## Güvenlik modeli

1. **Her write `confirm=true` ister.** Salt-okunur araçlarda koruma yok.
2. **Yıkıcı işlemler ek olarak `i_understand_data_loss=true` ister.** Bu
   wipe, destroy pool/VG, recursive zfs destroy, delete_snapshot ve
   `vm_exec`/`host_exec`'te komut destructive regex pattern eşleştiğinde
   uygulanır (`rm -rf`, `mkfs`, `dd of=/dev/`, `shutdown`, fork bombu,
   `zpool destroy`, `qm destroy`, vb.).
3. **SSH allow-list.** Faz 2.5 ve 3 SSH-tabanlı araçlar yalnızca sabit bir
   binary listesini çalıştırır (`wipefs`, `sgdisk`, `blkdiscard`, `dd`,
   `zfs`, `zpool`, LVM araçları, `lsblk`, `nvme`, `smartctl`). Her argüman
   gönderim öncesi `shlex.quote`'lanır.
4. **Serbest shell exec audit-logludur.** `proxmox_vm_exec` ve
   `proxmox_host_exec` her çağrıyı (alias, rc, komut, stdout/stderr ön
   izleme) paket yanındaki `_vm_ssh_audit.log` / `_host_ssh_audit.log`
   dosyalarına yazar.

## Gereksinimler

- **Python 3.11+**
- HTTPS üzerinden (varsayılan port 8006) erişilebilir bir Proxmox VE host
- Proxmox **API token** (aşağıda)
- Opsiyonel: Faz 2.5+ araçları için host'a yetkilendirilmiş SSH key
- Claude Desktop (veya herhangi bir MCP istemcisi)

## 1. Proxmox API token oluştur

1. Web UI → **Datacenter → Permissions → API Tokens → Add**
   - **User**: `root@pam` (veya ayrı kullanıcı)
   - **Token ID**: `mcp-server`
   - **Privilege Separation**: açık bırak
2. **Secret** değerini (UUID) hemen kopyala — bir daha görünmez.
3. Token'a rol ver: **Datacenter → Permissions → Add → API Token Permission**
   - **Path**: `/` (veya daha dar)
   - **Role**: `PVEAdmin` (tam) veya `PVEVMAdmin` (sadece VM/CT)
   - **Propagate**: işaretli

## 2. Kurulum

```bash
git clone https://github.com/ahmetem/proxmox-mcp.git ~/mcp-servers/proxmox-mcp
cd ~/mcp-servers/proxmox-mcp
python3 -m venv .venv
source .venv/bin/activate         # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. `.env` yapılandır

```ini
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_TOKEN_NAME=mcp-server
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-...
PROXMOX_VERIFY_SSL=false
PROXMOX_TIMEOUT=30

# Opsiyonel: yalnızca Faz 2.5+ SSH araçları ve vm/host exec için gerekir.
PROXMOX_SSH_HOST=                # boşsa PROXMOX_HOST kullanılır
PROXMOX_SSH_PORT=22
PROXMOX_SSH_USER=root
PROXMOX_SSH_KEY_PATH=/home/sen/.ssh/proxmox_ed25519
PROXMOX_SSH_KNOWN_HOSTS=         # known_hosts dosyası; güvenli LAN'da "ignore"
PROXMOX_SSH_PASSWORD=            # key yoksa fallback
PROXMOX_SSH_TIMEOUT=30
```

## 4. Opsiyonel: misafir VM SSH registry'si

`proxmox_vm_exec` / `proxmox_vm_read_file` misafir VM'lerde komut
çalıştırmak için alias kullanır. `.env` yanında `vm_ssh_hosts.json` oluştur
(dosya gitignore'da):

```json
{
  "_comment": "_ ile başlayan key'ler yorum sayılır ve görmezden gelinir.",
  "dockers": {
    "host": "192.168.1.20",
    "port": 22,
    "user": "ahmet",
    "key_path": "/home/sen/.ssh/vm_dockers_ed25519",
    "known_hosts": "ignore",
    "description": "Docker host VM 102"
  }
}
```

Claude `alias="dockers"`'ı buradaki kayda çözer. `known_hosts` bir dosya yolu
veya güvenli ağlarda `"ignore"` olabilir.

## 5. Claude Desktop'a kaydet

`claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`,
macOS: `~/Library/Application Support/Claude/`,
Linux: `~/.config/Claude/`):

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "C:\\mcp-servers\\proxmox-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\mcp-servers\\proxmox-mcp\\proxmox_mcp.py"],
      "cwd": "C:\\mcp-servers\\proxmox-mcp"
    }
  }
}
```

Claude Desktop'ı tamamen kapat (tray → Çıkış) ve yeniden aç.

## Örnek senaryolar

**Sağlık kontrolü:** *"Node durumunu ve %80 üstündeki storage'ı göster."*
Claude `proxmox_list_nodes`, `proxmox_list_storage`'ı çağırır, özetler.

**VM diskini taşı:** *"VM 102'nin scsi0'ını vmdata'dan nvmepool'a taşı,
orijinali unused olarak bırak."*
Claude `proxmox_move_disk` çağırır: `delete_source=false, confirm=true`.

**ZFS bakımı:** *"nvmepool için scrub başlat, sonra zpool status göster."*
Claude `proxmox_zfs_scrub` ve sonrasında `proxmox_zfs_pool_status` çağırır.

**Upgrade öncesi snapshot:** *"CT 200 için `pre-pg17-upgrade` snapshot al,
sonra içeride `apt list --upgradable` çalıştır."*
Claude `proxmox_create_snapshot`, sonra LXC alias'ına `proxmox_vm_exec`
çağırır.

## Proje yapısı

```
proxmox-mcp/
├── proxmox_mcp.py                  # eski Claude config'leri için shim
├── proxmox_mcp/                    # paket
│   ├── __init__.py / __main__.py
│   ├── server.py                   # FastMCP entry, TOOLS listesi
│   ├── config.py                   # env yükleme, require_config / require_ssh
│   ├── http_client.py              # async HTTPX wrapper'ları
│   ├── mcp_instance.py             # paylaşılan FastMCP
│   ├── models.py                   # paylaşılan Pydantic input modelleri
│   ├── format.py                   # fmt_bytes, status_icon, missing_confirm
│   ├── ssh.py                      # allow-listli SSH istemcisi
│   ├── host_ssh.py                 # serbest shell SSH (host)
│   ├── vm_ssh.py                   # serbest shell SSH (VM, registry)
│   └── tools/                      # her konu için ayrı modül
│       ├── nodes.py vms.py storage.py snapshots.py backups.py
│       ├── disks.py lvm.py zfs.py
│       ├── disks_prepare.py lvm_manage.py zfs_manage.py storage_manage.py
│       ├── ssh_disks.py ssh_zfs.py ssh_zfs_phase3.py
│       ├── vm_disk.py vm_ssh.py host_ssh.py
│       └── __init__.py
├── requirements.txt                # mcp, httpx, pydantic, python-dotenv, asyncssh
├── .env.example                    # .env şablonu
├── .gitignore                      # .env, vm_ssh_hosts.json, _*.{py,txt,log}
├── LICENSE                         # GPL v3
├── README.md                       # İngilizce sürüm
└── README.tr.md                    # bu dosya
```

## Sorun giderme

- **`Authentication failed. Check PROXMOX_TOKEN_VALUE.`** — secret yanlış
  veya kullanıcı yanlış (realm dahil olmalı, örn. `root@pam`).
- **`Permission denied. Token lacks privileges.`** — token'a uygun rolde bir
  API Token Permission ekle, `Propagate=checked`.
- **`wipedisk` / `initgpt` `user != root@pam` ile reddediliyor** — API
  token için bilinen Proxmox REST kısıtlaması. SSH eşdeğerlerini kullan
  (`proxmox_ssh_wipe_disk`, `proxmox_ssh_init_gpt`).
- **`Binary 'X' is not in the SSH allow-list.`** — tasarım gereği. Allow-list
  dışındaki bir şey için `proxmox_host_exec` kullan.
- **Araçlar Claude Desktop'ta görünmüyor.** — `%APPDATA%\Claude\logs\mcp*.log`
  (Windows) veya `~/Library/Logs/Claude/` (macOS) loglarına bak. En sık
  neden: `claude_desktop_config.json`'da yanlış yol veya çiftlenmemiş ters
  bölü.

## Katkı

Issue ve PR'lar açık. Bir araç eklerken:

1. Mevcut modül desenini takip et: `ConfigDict(extra="forbid")` ile bir
   Pydantic input model, annotations'lı `@mcp.tool` dekoratörü, ve
   `require_config()` (veya `require_ssh()`) koruyucusu.
2. Yıkıcı araçları `destructiveHint: True` ile işaretle ve input modelinde
   `confirm=True` zorunlu kıl. Geri dönüşsüz şeylere
   `i_understand_data_loss=True` ekle.
3. Araç adını `proxmox_mcp/server.py`'ın `TOOLS` listesine ekle ve modülü
   `proxmox_mcp/tools/__init__.py`'a kaydet.
4. Bu README'deki araç tablosunu güncel tut.

## Lisans

[GNU General Public License v3.0](./LICENSE) — tam metin için `LICENSE`
dosyasına bak.
