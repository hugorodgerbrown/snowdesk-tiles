# Dockerfile — minimal Caddy image for the self-hosted tile origin (SNOW-485).
#
# The image carries only the Caddyfile; the served assets (style, sprites,
# glyphs, and the multi-GB .pmtiles extract) live on the Render persistent
# disk mounted at /data, NOT baked into the image. See infra/tiles/README.md
# and docs/runbooks/self-hosted-tiles.md for how the disk is populated.

FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile

# Render injects $PORT; the Caddyfile binds to it. No EXPOSE needed — Render
# routes to whatever port the process listens on.
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
