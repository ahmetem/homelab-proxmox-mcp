# Proxmox MCP Sunucusu

Claude Desktop'ın (veya MCP destekli herhangi bir istemcinin) bir
**Proxmox VE** kümesini, token kimlik doğrulamasıyla REST API üzerinden
yönetmesini sağlayan yerel bir [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
sunucusu.

Tek-node bir kurulumda **Proxmox VE 9.1.9** ile test edilmiştir. Çok-node
kümelerde de çalışır — her araç bir `node` parametresi alır.

> 🇬🇧 English README: [README.md](./README.md)

## Özellikler

Paket, dokuz faz (0–7, ayrıca bir "2.5") içinde **63 araç** sunar;
aşağıda kategoriye göre gruplanmıştır. Salt-okunur araçlar otomatik
çağrılabilir; durum değiştiren her araç `confirm=true`, kalıcı veri silen
araçlar ayrıca `i_understand_data_loss=true` gerektirir (bkz. **Güvenlik**).

### Küme & node'lar (salt-okunur)

| Araç | Açıklama |
|---|---|
| `proxmox_list_nodes` | Kümedeki tüm node'ları durum, uptime, CPU ve RAM ile listeler |
| `proxmox_get_node_status` | Tek node için ayrıntılı durum: CPU modeli, kernel, load avg, disk, swap |

### VM'ler & container'lar

| Araç | Açıklama |
|---|---|
| `proxmox_list_vms` | Kümedeki tüm VM ve LXC container'ları listeler (salt-okunur) |
| `proxmox_get_vm_status` | Belirli bir VM/CT için ayrıntılı runtime metrikleri (salt-okunur) |
| `proxmox_vm_start` | VM veya LXC container başlatır |
| `proxmox_vm_shutdown` | Düzgün (ACPI) kapatma |
| `proxmox_vm_stop` | Zorla durdurma (fişi çekme) — veri kaybına yol açabilir |
| `proxmox_vm_reboot` | Önce graceful, sonra gerekirse power-cycle ile yeniden başlatma |
| `proxmox_resize_vm` | VM/CT'nin RAM (`memory_mb`) ve/veya CPU `cores` değerini değiştirir |

### Snapshot'lar

| Araç | Açıklama |
|---|---|
| `proxmox_list_snapshots` | Belirli bir VM/CT'nin snapshot'ları (salt-okunur) |
| `proxmox_create_snapshot` | VM/CT için snapshot oluşturur |
| `proxmox_rollback_snapshot` | Snapshot'a dönüş — sonraki veriler kaybolur |
| `proxmox_delete_snapshot` | Bir VM/CT'nin adlandırılmış snapshot'ını siler |

### Yedekler

| Araç | Açıklama |
|---|---|
| `proxmox_list_backups` | Bir storage'daki yedek dosyaları (salt-okunur) |
| `proxmox_create_backup` | Seçilebilir mod ve sıkıştırma ile yedek oluşturur |
| `proxmox_restore_backup` | Bir yedek arşivinden VM/CT geri yükler. Mevcut VMID'nin üzerine yazılması `force=true` ve `i_understand_data_loss=true` olmadıkça reddedilir. |

### Storage (salt-okunur)

| Araç | Açıklama |
|---|---|
| `proxmox_list_storage` | Node üzerindeki storage pool'lar ve kullanım bilgisi |
| `proxmox_storage_usage_detail` | Tek bir storage için tür bazında içerik dökümü: toplam boyutlar + en büyük N item. Kapasite planlaması için (örn. "hangi VM backup storage'ımı yiyor?"). |

### Phase 1 — disk / LVM / ZFS envanteri (salt-okunur)

| Araç | Açıklama |
|---|---|
| `proxmox_list_disks` | Node üzerindeki fiziksel diskler: model, boyut, tür, sağlık, kullanım |
| `proxmox_get_disk_smart` | Bir fiziksel disk için SMART sağlık ve attribute'ları |
| `proxmox_list_lvm` | Node üzerindeki LVM volume group ve logical volume'lar |
| `proxmox_list_lvm_thin` | Node üzerindeki LVM-thin pool'lar |
| `proxmox_list_zfs` | Node üzerindeki ZFS pool'lar (kapasite/sağlık ile) |
| `proxmox_get_zfs_pool` | Tek bir ZFS pool'un ayrıntılı durumu |

### Phase 2 — disk hazırlama & storage provisioning

| Araç | Açıklama |
|---|---|
| `proxmox_disk_init_gpt` | Boş bir diski GPT partition tablosu ile başlatır |
| `proxmox_wipe_disk` | Bir diskteki tüm partition/signature'ları siler (yıkıcı) |
| `proxmox_create_lvm_vg` | Bir disk üzerinde LVM volume group oluşturur |
| `proxmox_create_lvm_thin` | LVM-thin pool oluşturur |
| `proxmox_destroy_lvm_vg` | LVM volume group'u yok eder (yıkıcı) |
| `proxmox_destroy_lvm_thin` | LVM-thin pool'u yok eder (yıkıcı) |
| `proxmox_create_zfs_pool` | Bir veya daha çok disk üzerinde ZFS pool oluşturur |
| `proxmox_destroy_zfs_pool` | ZFS pool'u yok eder (yıkıcı) |
| `proxmox_list_cluster_storage` | Datacenter seviyesi storage yapılandırmasını listeler (salt-okunur) |
| `proxmox_add_zfs_storage` | Mevcut bir ZFS pool'u Proxmox storage olarak kaydeder |
| `proxmox_add_dir_storage` | Bir dizini Proxmox storage olarak kaydeder |
| `proxmox_remove_storage` | Datacenter config'inden bir storage girdisini kaldırır |

### Phase 2.5 — SSH tabanlı ZFS / disk işlemleri

Bunlar, API token'ının yapamadığı işlemler için node üzerinde SSH ile
shell komutu çalıştırır. [Yapılandırma referansı](#yapılandırma-referansı)
bölümündeki SSH ayarlarını gerektirir.

| Araç | Açıklama |
|---|---|
| `proxmox_ssh_wipe_disk` | Bir diski SSH üzerinden siler (yıkıcı) |
| `proxmox_ssh_init_gpt` | SSH üzerinden GPT label oluşturur |
| `proxmox_zfs_create_dataset` | ZFS dataset/filesystem oluşturur |
| `proxmox_zfs_destroy_dataset` | ZFS dataset'i yok eder (yıkıcı) |
| `proxmox_zfs_set_property` | Bir ZFS dataset'inde property ayarlar |
| `proxmox_zfs_create_snapshot` | Bir dataset'in ZFS snapshot'ını oluşturur |
| `proxmox_zfs_list_datasets` | ZFS dataset'lerini listeler (salt-okunur) |
| `proxmox_zfs_destroy_snapshots_by_pattern` | Glob pattern'ine uyan ZFS snapshot'larını toplu siler. İki aşamalı: `dry_run=true` (varsayılan) eşleşmeleri listeler; `dry_run=false` + `confirm=true` + `i_understand_data_loss=true` ile gerçek silme yapılır. `max_delete` (varsayılan 1000) ile sınırlanmıştır. |

### Phase 3 — VM disk / clone / ISO + ZFS durumu

| Araç | Açıklama |
|---|---|
| `proxmox_move_disk` | Bir VM diskini başka bir storage'a taşır |
| `proxmox_clone_vm` | Bir VM/CT'yi yeni bir VMID'ye klonlar |
| `proxmox_list_isos` | Storage'lardaki mevcut ISO imajlarını listeler (salt-okunur) |
| `proxmox_zfs_get_property` | Bir ZFS dataset/pool'dan property okur (salt-okunur) |
| `proxmox_zfs_pool_status` | Bir pool için `zpool status`: sağlık, hatalar, scrub durumu (salt-okunur) |
| `proxmox_zfs_scrub` | Bir ZFS pool'da scrub başlatır |
| `proxmox_zfs_send` | Replikasyon için ZFS send stream'i |

### Phase 4 — guest VM SSH (full shell, audit-logged)

| Araç | Açıklama |
|---|---|
| `proxmox_vm_list_hosts` | `vm_ssh_hosts.json`'da kayıtlı guest VM alias'larını listeler (salt-okunur) |
| `proxmox_vm_exec` | Kayıtlı bir guest VM'de SSH üzerinden shell komutu çalıştırır (`vm_ssh_hosts.json` kullanır). `confirm=true` gerektirir. |
| `proxmox_vm_read_file` | Kayıtlı bir guest VM'den SSH ile dosya okur (salt-okunur) |

### Phase 5 — Proxmox host SSH (full shell, audit-logged)

| Araç | Açıklama |
|---|---|
| `proxmox_host_exec` | Proxmox host'ta SSH üzerinden serbest shell komutu çalıştırır. `confirm=true` gerektirir; yıkıcı pattern'lere uyan komutlar ayrıca `i_understand_data_loss=true` ister. Her çağrı `_host_ssh_audit.log`'a yazılır. |

### Phase 6 — LXC exec

| Araç | Açıklama |
|---|---|
| `proxmox_lxc_exec` | Proxmox host'tan `pct exec` ile bir LXC container içinde shell komutu çalıştırır. CT içinde SSH gerektirmez. `confirm=true` gerektirir. |
| `proxmox_ct_service_action` | `proxmox_lxc_exec` üzerine typed kısayol: bir CT içinde `systemctl --no-pager <action> <service>`. Salt-okunur action'lar (status, is-active, is-enabled, show) `confirm` istemez; durum değiştirenler (start, stop, restart, reload, enable, disable, mask, unmask) `confirm=true` gerektirir. |
| `proxmox_ct_log_tail` | Bir CT içinden journald unit veya log dosyası tail eden salt-okunur araç. İki mod: `service` (journalctl -u) ve `file` (tail -n). Opsiyonel `grep` server-side filter. |

### Phase 7 — tanılama, adli inceleme & temizlik

| Araç | Açıklama |
|------|----------|
| `proxmox_zfs_list_snapshots` | ZFS snapshot'larını (name, used, referenced, creation) doğrudan SSH ile listeler; PVE config-snapshot API'sinin hiç göstermediği geçici `@vzdump` scratch snapshot'ları **dahil**. `dataset` ve/veya `contains` ile filtrelenir. (salt-okunur) |
| `proxmox_list_tasks` | Son PVE task'larını (vzdump/snapshot/...) başlangıç zamanı, durum ve UPID ile listeler. `typefilter`, `vmid`, `errors_only` ile filtrelenir. (salt-okunur) |
| `proxmox_get_task_log` | Bir PVE task'ının log satırlarını UPID ile okur — ör. bir `vzdump` çalışmasının tam olarak neden başarısız olduğunu. (salt-okunur) |
| `proxmox_list_backup_jobs` | `/cluster/backup`'tan yapılandırılmış `vzdump` job'larını (schedule, storage, vmid, retention) listeler. (salt-okunur) |
| `proxmox_cleanup_vzdump_snapshots` | **Self-heal.** Bir guest'in yedeklerini bloke eden artık `@vzdump` ZFS snapshot'larını temizler (yarıda kalan bir çalışmadan kalan scratch snapshot, sonraki tüm yedeklerin `dataset already exists` ile patlamasına yol açar). **Yalnızca** `@vzdump` ile biten isimleri hedefler ve `min_age_minutes`'ten (varsayılan 30) genç olanları atlar; çalışan bir yedeği asla kesmez. Varsayılan `dry_run=true`; gerçek silme için `dry_run=false` + `confirm=true` gerekir. |

### Güvenlik

Yıkıcı veya durum değiştiren tüm eylemler `confirm=true` gerektirir.
Kalıcı veri silen araçlar (snapshot delete, üzerine yazma ile backup
restore, disk wipe, LVM/ZFS pool/dataset destroy, bulk snapshot destroy
vb.) ek olarak `i_understand_data_loss=true` gerektirir. Ajanın iki
bayrağı da açıkça geçmesi gerekir — pratikte bu, Claude'un bu araçları
yalnızca kullanıcı eylemi açıkça istediğinde çağırması anlamına gelir.
Salt-okunur araçlarda böyle bir koruma yoktur.

## Gereksinimler

- **Python 3.11+**
- HTTPS üzerinden (varsayılan port 8006) erişilebilen bir Proxmox VE host
- Doğru yetkilere sahip bir Proxmox **API token**'ı (aşağıda anlatılıyor)
- Claude Desktop (veya herhangi bir MCP istemcisi)

## 1. Proxmox API token'ı oluştur

Sunucu, root parolasıyla değil API token'ı ile kimlik doğrular. Token'lar
tek tek iptal edilebilir ve etki alanını sınırlar.

1. Proxmox web arayüzüne giriş yap.
2. **Datacenter → Permissions → API Tokens**'a git.
3. **Add**'e tıkla:
   - **User**: `root@pam` (veya tercihen ayrı bir kullanıcı)
   - **Token ID**: `mcp-server` (istediğin bir isim)
   - **Privilege Separation**: aksini istemiyorsan açık bırak
4. **Add**'e bas. Açılan pencere **secret** değerini (bir UUID) **bir kez**
   gösterir. Hemen kopyala — bir daha alamazsın.

Privilege Separation açıksa token'a ayrıca yetki vermen gerekir.
**Datacenter → Permissions → Add → API Token Permission**:

- **Path**: `/` (veya daha dar)
- **API Token**: az önce oluşturduğun token
- **Role**: tam erişim için `PVEAdmin`, yalnızca VM/CT yönetimi için
  `PVEVMAdmin`
- **Propagate**: işaretli

Üretimde bunu çok daha dar kapsamlı yapabilirsin. Homelab için `/` +
`PVEAdmin` en basit yoldur.

## 2. Sunucuyu kur

### Windows (PowerShell)

```powershell
git clone https://github.com/<kullanici-adin>/proxmox-mcp.git C:\mcp-servers\proxmox-mcp
cd C:\mcp-servers\proxmox-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

PowerShell aktivasyon script'ini engelliyorsa, yönetici olarak açtığın bir
PowerShell'de bir defa şunu çalıştır:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Linux / macOS

```bash
git clone https://github.com/<kullanici-adin>/proxmox-mcp.git ~/mcp-servers/proxmox-mcp
cd ~/mcp-servers/proxmox-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. `.env` dosyasını yapılandır

```powershell
copy .env.example .env
notepad .env
```

Doldur:

```ini
PROXMOX_HOST=192.168.1.10            # Proxmox host'unun IP'si veya hostname'i
PROXMOX_PORT=8006                    # varsayılan
PROXMOX_USER=root@pam                # token'ın ait olduğu kullanıcı
PROXMOX_TOKEN_NAME=mcp-server        # seçtiğin Token ID
PROXMOX_TOKEN_VALUE=xxxxxxxx-xxxx-...  # gizli UUID
PROXMOX_VERIFY_SSL=false             # çoğu homelab self-signed kullanır
PROXMOX_TIMEOUT=30
```

`.env` dosyasını **asla** git'e commit etme. `.gitignore` zaten hariç tutar.

## 4. Hızlı test

venv aktifken:

```powershell
python proxmox_mcp.py --help
```

Araç listesini görüp temiz çıkmalı. Burada import hatası alıyorsan bir
bağımlılık doğru yüklenmemiş demektir.

## 5. Claude Desktop'a kaydet

Claude Desktop'ın config dosyasını aç:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Dosya yoksa oluştur. `mcpServers` bloğuna ekle (varsa genişlet):

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

Yolları kendi sistemine göre ayarla. Windows'ta JSON içinde ters bölü iki
katı olmalı (`\\`).

Claude Desktop'ı tamamen kapat (tray ikonu → Çıkış) ve yeniden aç. Yeni bir
sohbette Proxmox araçları çekiç/connector ikonunda görünür.

## Sohbetteki ilk test

Salt-okunur bir çağrıyla başla:

> "Proxmox node'larımı listele."

Claude `proxmox_list_nodes`'u çağırır ve node'ların listesini gösterir.
Kimlik doğrulama hatası alırsan `PROXMOX_TOKEN_VALUE`'yi ve token'ın
yetkilerini tekrar kontrol et.

Sonra dene:

> "Tüm VM'leri göster."
>
> "VM 101'in durumu ne?"
>
> "pve node'unun local storage'ındaki yedekleri listele."

Salt-okunur araçların düzgün çalıştığından emin olduğunda eylem araçlarını
deneyebilirsin:

> "VM 101'i yeniden başlat."

Claude onay isteyecek. Onayladıktan sonra `proxmox_vm_reboot`'u
`confirm=true` ile çağırır.

## Örnek senaryolar

**VM'i yeniden boyutlandır ve restart et:**

> "VM 101'i 4 GB RAM'e ayarla, sonra yeniden başlat."

Claude `proxmox_resize_vm`'i `memory_mb=4096, confirm=true` ile, sonra
`proxmox_vm_reboot`'u `confirm=true` ile çağırır.

**Riskli güncelleme öncesi hızlı snapshot:**

> "VM 102 için `pre-upgrade` adında snapshot oluştur, açıklaması
> 'kernel güncellemesi öncesi'."

Claude `proxmox_create_snapshot`'u `confirm=true` ile çağırır.

**Sağlık kontrolü:**

> "%80'in üzerinde dolu storage var mı?"

Claude `proxmox_list_storage`'ı çağırır ve özetler.

## Yapılandırma referansı

Tüm ayarlar `.env`'den okunan ortam değişkenlerinden gelir:

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PROXMOX_HOST` | — (zorunlu) | Proxmox host'unun IP veya hostname'i |
| `PROXMOX_PORT` | `8006` | API portu |
| `PROXMOX_USER` | — (zorunlu) | Token sahibi kullanıcı (örn. `root@pam`) |
| `PROXMOX_TOKEN_NAME` | — (zorunlu) | API token ID |
| `PROXMOX_TOKEN_VALUE` | — (zorunlu) | API token secret (UUID) |
| `PROXMOX_VERIFY_SSL` | `false` | API'nin TLS sertifikasını doğrula |
| `PROXMOX_TIMEOUT` | `30` | HTTP timeout (saniye) |

### SSH (opsiyonel — Phase 2.5 / 4 / 5 / 6 araçları için gerekli)

SSH tabanlı araçlar Proxmox host'ta (ve `proxmox_vm_*` için guest VM'lerde)
shell komutu çalıştırır. SSH yapılandırılana kadar pasiftirler.
`PROXMOX_SSH_HOST`, `PROXMOX_HOST`'a varsayılanır. Key auth tercih edilir;
hem key hem parola ayarlıysa key kullanılır. Guest VM'ler ayrıca
`vm_ssh_hosts.json`'da tanımlanır.

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PROXMOX_SSH_HOST` | = `PROXMOX_HOST` | Host seviyesi SSH araçları için host |
| `PROXMOX_SSH_PORT` | `22` | SSH portu |
| `PROXMOX_SSH_USER` | `root` | SSH kullanıcısı |
| `PROXMOX_SSH_KEY_PATH` | — | Private key yolu (tercih edilen auth) |
| `PROXMOX_SSH_PASSWORD` | — | Parola auth (key yoksa yedek) |
| `PROXMOX_SSH_KNOWN_HOSTS` | — | known_hosts dosyası yolu; `ignore` host-key doğrulamasını atlar (yalnızca güvenilir LAN) |
| `PROXMOX_SSH_TIMEOUT` | `30` | SSH bağlantı timeout'u (saniye) |

## Güvenlik notları

- Token secret'ı `.env`'de duruyor. Bu dosyayı yalnızca kendi kullanıcına
  okunabilir yap (Windows'ta `icacls`, Linux'ta `chmod 600`).
- Proxmox API'sini asla internete açma. Güvenilir bir LAN/VLAN'da veya
  VPN arkasında tut.
- Token'ı privilege-separated tut. Mümkünse root olmayan kullanıcı kullan.
- Eylem araçları `confirm=true` gerektirir. Bu korumayı kaldırma.
- Varsayılan `PROXMOX_VERIFY_SSL=false`, çünkü homelab sertifikaları
  genellikle self-signed olur. Güvenilir bir sertifika kurduysan `true` yap.

## Sorun giderme

- **"Authentication failed. Check PROXMOX_TOKEN_VALUE."**
  Token secret yanlış veya kullanıcı yanlış. Kullanıcı, token'ın
  oluşturulduğu kullanıcı ile eşleşmeli (örn. sadece `root` değil
  `root@pam`).

- **"Permission denied. Token lacks privileges."**
  Privilege separation açık ama token'a rol vermemiş olabilirsin.
  **Datacenter → Permissions**'a git ve bir **API Token Permission** ekle.

- **"Cannot connect to <host>:8006"**
  Ağ sorunu. Host'a ping at. İki taraftaki firewall'u kontrol et. Aynı
  makineden `https://<host>:8006/` adresinin açıldığını doğrula.

- **"Request timed out after 30s"**
  `PROXMOX_TIMEOUT`'u artır veya Proxmox node'unun aşırı yüklü olmadığını
  kontrol et.

- **Araçlar Claude Desktop'ta görünmüyor.**
  Hatalar için `%APPDATA%\Claude\logs\mcp*.log` (Windows) veya
  `~/Library/Logs/Claude/mcp*.log` (macOS) loglarına bak. En sık neden
  `claude_desktop_config.json`'da yanlış yol veya çiftlenmemiş ters bölü.

## Proje yapısı

```
proxmox-mcp/
├── proxmox_mcp/             # MCP sunucu paketi
│   ├── __main__.py          # `python -m proxmox_mcp` girişi
│   ├── server.py            # giriş noktası + tam araç kaydı (TOOLS)
│   ├── mcp_instance.py      # paylaşılan FastMCP örneği
│   ├── config.py            # ortam değişkeni yapılandırması
│   ├── http_client.py       # Proxmox REST istemcisi
│   ├── ssh.py / host_ssh.py / vm_ssh.py   # SSH backend'leri (host + guest)
│   └── tools/               # araç grubu başına bir modül (nodes, vms,
│                            #   disks, lvm, zfs, backups, ct_ops, …)
├── proxmox_mcp.py           # uyumluluk shim'i (`python proxmox_mcp.py`)
├── requirements.txt         # Python bağımlılıkları
├── .env.example             # Yerel .env için şablon
├── vm_ssh_hosts.json        # (git-ignore'lu) guest VM SSH kaydı
├── .gitignore
├── LICENSE                  # GPL v3
├── README.md                # İngilizce sürüm
└── README.tr.md             # Bu dosya
```

## Katkı

Issue ve PR'lara açık. Bir araç eklersen lütfen:

1. Mevcut deseni takip et: pydantic input modeli + `_require_config` +
   hata yönetimi.
2. Yıkıcı araçları annotations'ta `destructiveHint: True` ile işaretle ve
   input modelinde `confirm=True` zorunlu kıl.
3. Bu README'deki araç listesini güncelle.

## Lisans

[GNU General Public License v3.0](./LICENSE) — tam metin için `LICENSE`
dosyasına bak.
