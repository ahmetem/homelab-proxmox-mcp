# Proxmox MCP Sunucusu

Claude Desktop'ın (veya MCP destekli herhangi bir istemcinin) bir
**Proxmox VE** kümesini, token kimlik doğrulamasıyla REST API üzerinden
yönetmesini sağlayan yerel bir [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
sunucusu.

Tek-node bir kurulumda **Proxmox VE 9.1.9** ile test edilmiştir. Çok-node
kümelerde de çalışır — her araç bir `node` parametresi alır.

> 🇬🇧 English README: [README.md](./README.md)

## Özellikler

Paket dokuz faz içinde 58 araç sunar. Çekirdek/"Phase 0" yüzeyi aşağıda
kategoriye göre özetlenmiştir; disk hazırlama, LVM, ZFS, guest SSH,
host SSH, LXC exec ve bulk snapshot temizleme için ileri seviye
araçlar da kayıtlıdır — tam liste için `python proxmox_mcp.py --help`
çalıştır.

### Salt-okunur (otomatik çağrı güvenli)

| Araç | Açıklama |
|---|---|
| `proxmox_list_nodes` | Kümedeki tüm node'ları durum, uptime, CPU ve RAM ile listeler |
| `proxmox_get_node_status` | Tek node için ayrıntılı durum: CPU modeli, kernel, load avg, disk, swap |
| `proxmox_list_vms` | Kümedeki tüm VM ve LXC container'ları listeler |
| `proxmox_get_vm_status` | Belirli bir VM/CT için ayrıntılı runtime metrikleri |
| `proxmox_list_storage` | Node üzerindeki storage pool'lar ve kullanım bilgisi |
| `proxmox_storage_usage_detail` | Tek bir storage için tür bazında içerik dökümü: toplam boyutlar + en büyük N item. Kapasite planlaması için (örn. "hangi VM backup storage'ımı yiyor?"). |
| `proxmox_list_backups` | Bir storage'daki yedek dosyaları |
| `proxmox_list_snapshots` | Belirli bir VM/CT'nin snapshot'ları |

### Güç eylemleri (`confirm=true` gerektirir)

| Araç | Açıklama |
|---|---|
| `proxmox_vm_start` | VM veya LXC container başlatır |
| `proxmox_vm_shutdown` | Düzgün (ACPI) kapatma |
| `proxmox_vm_stop` | Zorla durdurma (fişi çekme) — veri kaybına yol açabilir |
| `proxmox_vm_reboot` | Önce graceful, sonra gerekirse power-cycle ile yeniden başlatma |

### Snapshot ve yedekleme (`confirm=true` gerektirir)

| Araç | Açıklama |
|---|---|
| `proxmox_create_snapshot` | VM/CT için snapshot oluşturur |
| `proxmox_rollback_snapshot` | Snapshot'a dönüş — sonraki veriler kaybolur |
| `proxmox_create_backup` | Seçilebilir mod ve sıkıştırma ile yedek oluşturur |
| `proxmox_restore_backup` | Bir yedek arşivinden VM/CT geri yükler. Mevcut VMID'nin üzerine yazılması `force=true` ve `i_understand_data_loss=true` olmadıkça reddedilir. |

### Yapılandırma (`confirm=true` gerektirir)

| Araç | Açıklama |
|---|---|
| `proxmox_resize_vm` | VM/CT'nin RAM (`memory_mb`) ve/veya CPU `cores` değerini değiştirir |

### Guest exec (Phase 4 / 6 — full shell, audit-logged)

| Araç | Açıklama |
|---|---|
| `proxmox_vm_exec` | Kayıtlı bir guest VM'de SSH üzerinden shell komutu çalıştırır (`vm_ssh_hosts.json` kullanır). `confirm=true` gerektirir. |
| `proxmox_lxc_exec` | Proxmox host'tan `pct exec` ile bir LXC container içinde shell komutu çalıştırır. CT içinde SSH gerektirmez. `confirm=true` gerektirir. |
| `proxmox_ct_service_action` | `proxmox_lxc_exec` üzerine typed kısayol: bir CT içinde `systemctl --no-pager <action> <service>`. Salt-okunur action'lar (status, is-active, is-enabled, show) `confirm` istemez; durum değiştirenler (start, stop, restart, reload, enable, disable, mask, unmask) `confirm=true` gerektirir. |
| `proxmox_ct_log_tail` | Bir CT içinden journald unit veya log dosyası tail eden salt-okunur araç. İki mod: `service` (journalctl -u) ve `file` (tail -n). Opsiyonel `grep` server-side filter. |

### Toplu ZFS bakımı (Phase 2.5 — SSH tabanlı)

| Araç | Açıklama |
|---|---|
| `proxmox_zfs_destroy_snapshots_by_pattern` | Glob pattern'ine uyan ZFS snapshot'larını toplu siler. İki aşamalı: `dry_run=true` (varsayılan) eşleşmeleri listeler; `dry_run=false` + `confirm=true` + `i_understand_data_loss=true` ile gerçek silme yapılır. `max_delete` (varsayılan 1000) ile sınırlanmıştır. |

### Güvenlik

Yıkıcı veya durum değiştiren tüm eylemler `confirm=true` gerektirir.
Kalıcı veri silen araçlar (snapshot delete, üzerine yazma ile backup
restore, bulk snapshot destroy, ZFS dataset destroy vb.) ek olarak
`i_understand_data_loss=true` gerektirir. Ajanın iki bayrağı da açıkça
geçmesi gerekir — pratikte bu, Claude'un bu araçları yalnızca kullanıcı
eylemi açıkça istediğinde çağırması anlamına gelir. Salt-okunur araçlarda
böyle bir koruma yoktur.

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
├── proxmox_mcp.py      # MCP sunucusu
├── requirements.txt    # Python bağımlılıkları
├── .env.example        # Yerel .env için şablon
├── .gitignore
├── LICENSE             # GPL v3
├── README.md           # İngilizce sürüm
└── README.tr.md        # Bu dosya
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
