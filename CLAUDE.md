# homelab-proxmox-mcp — proje talimatları

<!-- Hedef 40-80 satır. Codebase'den okunabileni yazma (araç listesi, dizin ağacı).
     YAZ: komut, koşum yeri, sapan konvansiyon, tuzak. -->

## Ne / nerede koşuyor
- **Ne:** Proxmox VE yönetimi için özel MCP sunucusu (PyPI/paket adı **`pve-mcp`**).
  Homelab'in tek node'unu yönetir: VM/CT yaşam döngüsü, ZFS, yedek/restore, disk, task,
  storage.
- **Nerede:** **Yerel makinede** stdio MCP olarak koşar — Claude Desktop config'inde
  `proxmox` adıyla kayıtlı (`python proxmox_mcp.py`). Sunucuya kurulu değil.
- **Sürüm yeri:** `pyproject.toml` + `CHANGELOG.md`
- **Kod keşfi:** CBM indeksli · graphify grafında var

## Komutlar

    python -m pytest              # tests/
    python proxmox_mcp.py         # stdio MCP; normalde Claude başlatır, elle nadiren

## Nereye bakılır
- Araçlar `proxmox_mcp/tools/` altında konu bazlı bölünmüş
- Ortak kapılar: `proxmox_mcp/format.py` → `missing_confirm()`,
  `proxmox_mcp/config.py` → `require_config()`,
  `proxmox_mcp/operator_ack.py` → `@gated(...)` + `ack_refusal(...)`
- **Araç envanterini `tools/` dizininden oku** — sayıyı buraya yazmıyorum, bayatlar
  (README'deki sayı da geride kalabiliyor)
- **`proxmox_mcp/mcp_instance.py` SDK'nın server sınıfına dokunan TEK modüldür**
  (spec revizyonu 2026-07-28): `MCPServer` importu `ImportError`'da 1.x'in `FastMCP`'sine
  düşer. Başka modüle `mcp.server.*` importu ekleme — 1.x'te sessizce kırılır.

## Veri ve bağımlılıklar
- **Zorunlu env:** `PROXMOX_HOST`, `PROXMOX_USER`, `PROXMOX_TOKEN_NAME`,
  `PROXMOX_TOKEN_VALUE`; opsiyonel `PROXMOX_{PORT,VERIFY_SSL,TIMEOUT}` — `.env.example`'da
- API **token** ile bağlanır, parola ile değil

## Bu projeye özgü kısıtlar
- **Yıkıcı araçlarda kapı İKİ KATMANLI (v1.5.0) — karıştırma:**
  (a) `confirm=true` bir **tool argümanıdır**, yani modelin niyetini kanıtlar,
  operatörünkini değil; kaldırılmaz, varsayılanı `true` yapılmaz.
  (b) `operator_ack` **insan** yarısıdır: resolver injection ile gelir, model-facing
  şemada görünmez, dolayısıyla tool çağrısıyla uydurulamaz. `mcp<2.0`'da ya da form
  elicitation sunmayan istemcide **`confirm`-only davranışına düşer** (araçlar
  kaybolmaz). Soru yalnız `confirm=true` zaten verilmişken sorulur — eksik bayrak
  operatörü rahatsız etmeden yerinde reddedilir.
  Yeni yıkıcı araç eklerken üçünü birden uygula: `missing_confirm()` deseni,
  `@mcp.tool(...)` **altına** `@gated(...)`, ve gövdede `ack_refusal(...)`.
- Bu sunucu **gerçek homelab'i** yönetir; sahte ortam yok. Yeni araç geliştirirken önce
  salt-okunur muadilini doğrula, yıkıcı yolu en son ve tek guest üzerinde dene.

## Tuzaklar
- Değişiklik geçmişi ve bilinen davranış farkları: `CHANGELOG.md`
- Araç eklendiğinde brain'deki proje notu ve `homelab-project/02-mcp-servers.md` bayatlar;
  haftalık `brain-repo-reconcile` görevi bunu yakalar.
