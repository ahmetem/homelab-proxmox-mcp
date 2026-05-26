# Proxmox MCP Sunucusu

Claude Desktop'ın (veya MCP destekli herhangi bir istemcinin) bir
**Proxmox VE** kümesini REST API üzerinden token kimlik doğrulamasıyla
— ve API'nin sunmadığı işlemler için isteğe bağlı bir SSH katmanıyla —
yönetmesini sağlayan yerel bir [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
sunucusu.

Tek-node bir kurulumda **Proxmox VE 9.1.9** ile test edilmiştir. Çok-node
kümelerde de çalışmalı — node gerektiren her araç bir `node` parametresi alır.

> U0001F1ECU0001F1E7 English README: [README.md](./README.md)

## Sunuyor

Beş faz halinde **52 araç**. Salt-okunur envanter, VM yaşam döngüsü,
snapshot, yedek, disk hazırlık, LVM/ZFS pool create/destroy, küme storage
yönetimi, SSH üzerinden ZFS dataset/property/snapshot, ZFS replication,
misafir VM'lerde ve Proxmox host'unda tam shell exec — hepsi `confirm=true`
ve gerektiğinde `i_understand_data_loss=true` koruyucusu arkasında.

### Araç yüzeyi, faz bazında

| Faz | Modül(ler) | Araç |
|---|---|---|
| 0 — Yaşam döngüsü | `nodes`, `vms`, `storage`, `snapshots`, `backups` | 16 |
| 1 — Envanter (salt-okunur) | `disks`, `lvm`, `zfs` | 6 |
| 2 — Disk hazırlık + pool create/destroy + küme storage | `disks_prepare`, `lvm_manage`, `zfs_manage`, `storage_manage` | 12 |
| 2.5 — SSH-tabanlı (API token kısıtlarını aşar) | `ssh_disks`, `ssh_zfs` | 7 |
| 3 — VM disk ops + ZFS read/scrub/send | `vm_disk`, `ssh_zfs_phase3` | 7 |
| 4 — Misafir VM SSH (audit log'lu shell exec) | `vm_ssh` | 3 |
| 5 — Proxmox host SSH (audit log'lu shell exec) | `host_ssh` | 1 |
| **Toplam** | 18 modül | **52** |

Tüm araç listesi için: `python proxmox_mcp.py --help`.

### Güvenlik modeli

- **Salt-okunur araçlar** asla onay gerektirmez.
- **Durum değiştiren araçlar** `confirm=true` gerektirir. Ajanın bu bayrağı
  açıkça geçmesi gerekir — pratikte Claude bunu ancak kullanıcı açıkça
  isteğini belirttikten sonra yapar.
- **Yıkıcı araçlar** (wipe, destroy, delete, zorla durdurma, destructive
  pattern eşleşen shell exec) ek olarak `i_understand_data_loss=true`
  gerektirir.
- **SSH allow-list ikili dosyaları** (`proxmox_mcp/ssh.py`): sadece
  `wipefs`, `sgdisk`, `blkdiscard`, `dd`, `zfs`, `zpool`, LVM araçları,
  `lsblk`, `nvme`, `smartctl`. `ssh_disks` ve `ssh_zfs*` modüllerinde
  kullanılır.
- **Serbest-shell araçları** (`proxmox_vm_exec`, `proxmox_host_exec`)
  ikili allow-list'i yoktur; her çağrı paket yanındaki
  `_vm_ssh_audit.log` veya `_host_ssh_audit.log` dosyasına yazılır. Bir
  regex kontrolü yıkıcı komutları işaretler; bypass için
  `i_understand_data_loss=true` gerekir.

## Gereksinimler

- Python 3.11+
- HTTPS üzerinden (varsayılan port 8006) erişilebilen bir Proxmox VE host
- Bir Proxmox API token'ı (REST araçları için)
- Proxmox host'unda yetkilendirilmiş bir SSH key (yalnızca Phase 2.5 ve
  Phase 3–5 araçları için)
- Claude Desktop veya başka bir MCP istemcisi

## 1. Proxmox API token'ı oluştur

1. Web UI → **Datacenter → Permissions → API Tokens → Add**.
2. User `root@pam` (veya ayrı kullanıcı), Token ID örneğin `mcp-server`.
3. Gizli UUID'yi kopyala — bir kez gösterilir.
4. **Datacenter → Permissions → Add → API Token Permission**: Path `/`,
   Role `PVEAdmin`, Propagate işaretli. (Üretimde daha dar tut.)

## 2. Kurulum

```powershell
git clone https://github.com/ahmetem/proxmox-mcp.git C:\mcp-servers\proxmox-mcp
cd C:\mcp-servers\proxmox-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/macOS için: `python3 -m venv .venv && source .venv/bin/activate`.

## 3. `.env` yapılandır

`.env.example`'ı `.env` olarak kopyala ve doldur. Asgari (yalnızca REST):

```ini
PROXMOX_HOST=192.168.1.21
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_TOKEN_NAME=mcp-server
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PROXMOX_VERIFY_SSL=false
PROXMOX_TIMEOUT=30
```

Isteğe bağlı (Phase 2.5 + 3–5 araçlarını açar):

```ini
PROXMOX_SSH_HOST=192.168.1.21        # boşsa PROXMOX_HOST kullanılır
PROXMOX_SSH_PORT=22
PROXMOX_SSH_USER=root
PROXMOX_SSH_KEY_PATH=C:\Users\you\.ssh\proxmox_ed25519
PROXMOX_SSH_KNOWN_HOSTS=             # known_hosts dosyası yolu, veya güvenilir LAN'da 'ignore'
PROXMOX_SSH_TIMEOUT=30
```

Key auth şiddetle tercih edilir. `PROXMOX_SSH_KNOWN_HOSTS=ignore` yalnızca
güvenilir bir LAN'da kullan; sunucu bunu gördüğünde stderr'e uyarı düşer.

### `vm_ssh_hosts.json` (sadece Phase 4)

`proxmox_vm_exec` / `proxmox_vm_read_file` için sunucu, `.env` yanındaki
`vm_ssh_hosts.json` dosyasından misafir VM SSH hedeflerinin JSON kayıt
çizelgesini okur. Git'e commit edilmez. Format:

```json
{
  "web01": {
    "host": "192.168.1.50",
    "port": 22,
    "user": "deploy",
    "key_path": "C:\\Users\\you\\.ssh\\web01_ed25519",
    "known_hosts": "ignore",
    "description": "Web uygulaması, NGINX + Node"
  },
  "db01": {
    "host": "192.168.1.51",
    "port": 22,
    "user": "postgres",
    "key_path": "C:\\Users\\you\\.ssh\\db01_ed25519",
    "description": "Postgres 16"
  }
}
```

Hangi aliasların göründüğünü doğrulamak için sohbette
`proxmox_vm_list_hosts` kullan.

## 4. Hızlı test

```powershell
python proxmox_mcp.py --help
```

52 aracın tam listesi görünmeli ve temiz çıkmalı.

## 5. Claude Desktop'a kaydet

`%APPDATA%\Claude\claude_desktop_config.json`:

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

Üst-seviye `proxmox_mcp.py` bir uyumluluk shim'i; eşdeğer olarak
`args: ["-m", "proxmox_mcp"]` da kullanılabilir. Claude Desktop'ı tamamen
kapat ve yeniden aç.

## Örnekler

```
Proxmox node'larımı listele.
# proxmox_list_nodes

Host hangi diskleri görüyor? Wearout göster.
# proxmox_list_disks

VM 102 için 'pre-upgrade' adında snapshot oluştur.
# proxmox_create_snapshot(confirm=true)

VM 102'nin scsi0'ını vmdata'dan nvmepool'a taşı, kaynağı silme.
# proxmox_move_disk(confirm=true, delete_source=false)

nvmepool'da scrub başlat.
# proxmox_zfs_scrub(confirm=true)

nvmepool/data@daily-2025-11 snapshot'ını vmdata/backup'a replicate et.
# proxmox_zfs_send(replication=true, confirm=true)

dockers VM'ini reboot et.
# proxmox_vm_exec(alias="dockers", command="sudo systemctl reboot",
#                 confirm=true, i_understand_data_loss=true)
```

## Yapılandırma referansı

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PROXMOX_HOST` | — (zorunlu) | Proxmox host IP/hostname |
| `PROXMOX_PORT` | `8006` | API portu |
| `PROXMOX_USER` | — (zorunlu) | Token sahibi (örn. `root@pam`) |
| `PROXMOX_TOKEN_NAME` | — (zorunlu) | API token ID |
| `PROXMOX_TOKEN_VALUE` | — (zorunlu) | API token gizli (UUID) |
| `PROXMOX_VERIFY_SSL` | `false` | API TLS sertifikasını doğrula |
| `PROXMOX_TIMEOUT` | `30` | HTTP timeout (saniye) |
| `PROXMOX_SSH_HOST` | `PROXMOX_HOST` | SSH hedefi (yalnızca SSH araçları için) |
| `PROXMOX_SSH_PORT` | `22` | SSH portu |
| `PROXMOX_SSH_USER` | `root` | SSH kullanıcısı |
| `PROXMOX_SSH_KEY_PATH` | — | Private key mutlak yolu (tercih edilen kimlik) |
| `PROXMOX_SSH_PASSWORD` | — | Key ayarlı değilse yedek |
| `PROXMOX_SSH_KNOWN_HOSTS` | — | known_hosts dosya yolu, veya `ignore` |
| `PROXMOX_SSH_TIMEOUT` | `30` | SSH timeout (saniye) |

## Proje yapısı

```
proxmox-mcp/
├── proxmox_mcp.py             # Uyumluluk shim'i (proxmox_mcp.server:main çağırır)
├── proxmox_mcp/
│   ├── __init__.py            # mcp ve main'i açıksa eder
│   ├── __main__.py            # `python -m proxmox_mcp`
│   ├── server.py              # Giriş noktası + TOOLS listesi
│   ├── config.py              # .env yükleme, require_config(), require_ssh()
│   ├── format.py              # fmt_bytes, status_icon, missing_confirm, …
│   ├── http_client.py         # Asenkron REST yardımcıları
│   ├── mcp_instance.py        # Paylaşılan FastMCP instance
│   ├── models.py              # Paylaşılan Pydantic input modelleri
│   ├── ssh.py                 # Allow-list'li SSH istemcisi
│   ├── host_ssh.py            # Proxmox host'unda serbest-shell SSH
│   ├── vm_ssh.py              # Misafir VM'lerde serbest-shell SSH (registry tabanlı)
│   └── tools/                 # Faza göre gruplandırılmış 18 araç modülü
├── requirements.txt           # mcp, httpx, pydantic, python-dotenv, asyncssh
├── .env.example               # Şablon; .env olarak kopyalayıp doldur
├── .gitignore                 # .env, vm_ssh_hosts.json, audit log'lar hariç
├── LICENSE                    # GPL v3
├── README.md                  # İngilizce sürüm
└── README.tr.md               # Bu dosya
```

## Sorun giderme

- **"Authentication failed. Check PROXMOX_TOKEN_VALUE."** — token gizli
  yanlış, veya kullanıcı eşleşmiyor. Token kullanıcısı birebir eşleşmeli
  (örn. sadece `root` değil `root@pam`).
- **"Permission denied. Token lacks privileges."** — privilege separation
  açık, rol atanmamış. **API Token Permission** ekle.
- **`wipedisk`/`initgpt` "user != root@pam" dönüyor** — Proxmox özelliği:
  REST endpoint'leri API token'ı reddediyor. SSH varyantlarını kullan:
  `proxmox_ssh_wipe_disk` / `proxmox_ssh_init_gpt`.
- **`zfs`/`zpool` komutları "binary not in allow-list" dönüyor** — SSH
  modülü allow-list zorlar. Gerçekten başka bir ikili gerekiyorsa
  `proxmox_mcp/ssh.py` içindeki `ALLOWED_BINARIES`'i düzenle ve PR gönder.
- **`vm_exec` "alias not in vm_ssh_hosts.json" diyor** — alias'ı `.env`
  yanındaki JSON'a ekle.
- **Araçlar Claude Desktop'ta görünmüyor** — import hatası için
  `%APPDATA%\Claude\logs\mcp*.log` (Windows) veya
  `~/Library/Logs/Claude/mcp*.log` (macOS) loglarına bak.

## Katkı

Issue ve PR'lar açık. Yeni bir araç için:

1. `proxmox_mcp/tools/` altındaki uygun faz modülünü seç, veya yeni bir
   modül oluştur ve `proxmox_mcp/tools/__init__.py`'da referansla.
2. Mevcut deseni takip et: strict regex/length doğrulamalı Pydantic
   input modeli, `require_config()` veya `require_ssh()` koruyucusu,
   yazma işlemlerinde `confirm=true`, yıkıcı işlemlerde
   `i_understand_data_loss=true`.
3. `proxmox_mcp/server.py` içindeki `TOOLS` listesine araç adını ekle.
4. Her iki README'yi de güncelle.

## Lisans

[GNU General Public License v3.0](./LICENSE) — tam metin için `LICENSE`
dosyasına bak.
