# Operasional: mesin tidak boleh sleep

Monitor berjalan sebagai proses user + MT4 (Wine). Saat sistem suspend, semua proses
dibekukan: tidak ada tick, engine tidak di-step, order tidak bisa dikirim, dan posisi
terbuka tidak terpantau (broker tidak diberi SL/TP saat `ATTACH_SL=0`, `ATTACH_TP=0`).

## Yang dipasang

1. Target sleep di-mask secara sistem (berlaku langsung, tanpa reboot):

   ```
   systemctl mask sleep.target suspend.target hibernate.target \
                    hybrid-sleep.target suspend-then-hibernate.target
   ```

   Efek: `systemctl suspend` dan permintaan suspend dari logind/GNOME gagal
   ("Unit suspend.target is masked") — mesin tetap hidup.

2. `/etc/systemd/logind.conf.d/99-no-suspend.conf`:

   ```
   [Login]
   HandleLidSwitch=ignore
   HandleLidSwitchExternalPower=ignore
   HandleLidSwitchDocked=ignore
   HandleSuspendKey=ignore
   HandleHibernateKey=ignore
   HandlePowerKey=ignore
   IdleAction=ignore
   ```

   Berlaku penuh setelah reboot (atau `systemctl restart systemd-logind`).
   Tanpa ini, tutup lid tetap *meminta* suspend — tetapi tetap diblokir oleh mask.

3. GNOME auto-suspend AC & baterai = `nothing` (default mesin ini).

## Konsekuensi

- Layar tetap boleh blank/terkunci; proses trading tetap jalan.
- Tutup lid **tidak** mematikan mesin: sambungkan charger, baterai akan terkuras.
- Power button tidak lagi mematikan mesin (HandlePowerKey=ignore):

  ```
  systemctl poweroff     # cara mematikan
  ```

## Verifikasi

```
systemctl is-enabled sleep.target suspend.target       # -> masked
systemctl --user is-active 'xau-monitor*'              # -> active
curl -s 'http://127.0.0.1:8772/api/status' | python3 -m json.tool | grep -E 'effective|mt4_age'
```

## Membatalkan

```
sudo rm /etc/systemd/logind.conf.d/99-no-suspend.conf
sudo systemctl unmask sleep.target suspend.target hibernate.target \
                     hybrid-sleep.target suspend-then-hibernate.target
```

## Batas jaminan

Mask hanya menahan suspend berbasis systemd. Suspend manual level kernel
(`echo mem > /sys/power/state`, root) dan hibernasi firmware tetap bisa terjadi.
Untuk uptime penuh, jalankan di host yang selalu menyala (VPS/NUC).
