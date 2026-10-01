# Project workflow

- Develop in this repository. Keep personal URLs, credentials, local paths, and
  deployment-specific configuration out of tracked files and image layers.
- `config.yml` is private and must remain ignored; maintain generic defaults in
  `config.example.yml`.
- After each project change, run `python3 scripts/sync_private.py` to sync to the
  sibling `ersatztv-jellyfin-bridge-mine` directory and rebuild/recreate its
  container. Preserve its config (apart from required port migrations), Git
  state, and private container name. Do not publish the private directory.
- Use port 8121 inside and outside the container and name the service
  `ersatztv-jellyfin-bridge`.
- Release tags and release titles must contain only the version, e.g. `v0.1.0`.
- Publish Docker images to GHCR for linux/amd64 and linux/arm64.
