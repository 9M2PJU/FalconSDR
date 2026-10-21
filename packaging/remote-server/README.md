# Headless RTL-SDR receiver

`smartscanatc-receiver.service` runs the scanner and recorder continuously on
the receiver.  It exposes an authenticated HTTP API on port 8765; the desktop
app uses it for Start, Stop, Skip, recording controls and downloading closed
WAV/MP3 files for local playback and Whisper transcription.

The service runs as an unprivileged `smartscan` user (config in
`/var/lib/smartscanatc/.config/SmartScanATC`, recordings in
`/var/lib/smartscanatc/SmartScanATC/recordings`). Create it once:

```sh
useradd --system --create-home --home-dir /var/lib/smartscanatc --groups plugdev smartscan
install -m 644 packaging/linux-deb/60-rtl-sdr.rules /etc/udev/rules.d/  # if no rtl-sdr package is installed
```

Upgrading from an older install that ran as root: move `/root/.config/SmartScanATC`
and `/root/SmartScanATC` into `/var/lib/smartscanatc/` and `chown -R smartscan:`
them, then `systemctl daemon-reload && systemctl restart smartscanatc-receiver`.

The API is plain HTTP: anyone who can see LAN traffic can read the token and
then control the receiver and delete recordings. Keep it on a trusted LAN, and
use a VPN (e.g. WireGuard) for anything beyond that.

The receiver must have the RTL-SDR USB device passed into its Linux instance.
For a Proxmox LXC container, run this on the Proxmox host (replace `CTID`):

```sh
pct set CTID -usb0 host=0bda:2838
pct restart CTID
```

For an LXD container, attach it from the LXD host instead:

```sh
lxc config device add INSTANCE rtlsdr usb vendorid=0bda productid=2838
```

The service will reconnect by itself after the device appears.  Verify it
inside the receiver with `rtl_test -t` and `systemctl status
smartscanatc-receiver`.
