# Exposing the station API with a Cloudflare Tunnel

The Vercel site needs to reach the station's FastAPI server (`/api/printers`,
`/api/printer/<serial>/camera`) over the public internet, but the station lives
on a laptop on the makerspace LAN behind NAT. A **Cloudflare Tunnel** is the
simplest free way to do this without opening router ports or a dynamic DNS.

## 1. Install cloudflared (Windows)

Download the official binary from
<https://github.com/cloudflare/cloudflared/releases/latest> and place
`cloudflared.exe` somewhere on `PATH`, or use winget:

```powershell
winget install --id Cloudflare.cloudflared
```

## 2. Authenticate

```powershell
cloudflared tunnel login
```

This opens a browser to authorize cloudflared against a Cloudflare account. You
need a free Cloudflare account and a domain managed by Cloudflare (a cheap `.tech`
or `.xyz` domain works, or a subdomain of an existing one).

## 3. Create a named tunnel

```powershell
cloudflared tunnel create makerlab-status
```

This prints a tunnel UUID and writes a credentials file. Note the UUID.

## 4. Route a hostname to the tunnel

Pick a hostname, e.g. `makerlab-status.yourdomain.com`, and add a DNS CNAME that
points at the tunnel:

```powershell
cloudflared tunnel route dns makerlab-status makerlab-status.yourdomain.com
```

## 5. Configure the tunnel

Create `C:\Users\<you>\.cloudflared\config.yml`:

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: C:\Users\<you>\.cloudflared\<TUNNEL_UUID>.json

ingress:
  - hostname: makerlab-status.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404
```

`localhost:8080` matches `web.api_port` in `config.json` (default 8080), where
`main.py` starts the FastAPI server.

## 6. Run it (and survive reboot)

Test once:

```powershell
cloudflared tunnel run makerlab-status
```

Then install it as a Windows service so it starts at boot:

```powershell
cloudflared service install
```

Start the service:

```powershell
sc start cloudflared
```

## 7. Point the project at the tunnel

In `config.json`:

```json
"web": {
  "station_api_url": "https://makerlab-status.yourdomain.com",
  "api_host": "127.0.0.1",
  "api_port": 8080
}
```

And in `web/.env.local`:

```
STATION_API_URL=https://makerlab-status.yourdomain.com
```

> Set `api_host` to `127.0.0.1` so the FastAPI server only listens on the
> loopback interface — the tunnel is the only thing that should reach it from
> outside the laptop. `0.0.0.0` is fine too if you also want LAN devices to hit
> it directly.

## Alternatives

If you don't have a Cloudflare-managed domain:

- **Tailscale Funnel** — `tailscale funnel 8080` exposes the port over Tailscale's
  public edge with a `*.ts.net` URL. Easiest if you already use Tailscale.
- **ngrok** — `ngrok http 8080` gives a random public URL instantly; a paid plan
  gives a stable hostname. Good for quick demos, less ideal for a permanent lab
  install.
- **Port forwarding** — forward a public port to the laptop's LAN IP. Works but
  depends on your network and is usually blocked on campus networks.

## Locking it down (optional)

The station API currently has no auth. If you want to gate it:

- Add a shared secret header check in `station/api_server.py` (a small
  dependency that compares `request.headers.get("x-station-key")` to a value
  from `config.json`), and have the Vercel site send it from an environment
  variable.
- Or use Cloudflare Access (Zero Trust) in front of the tunnel hostname for
  SSO-gated access to the status API only.
