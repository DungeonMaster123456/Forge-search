# Self-hosted SearXNG for Forge-Search's web-fallback tier.
# Deploys the OFFICIAL SearXNG image with JSON output enabled — public
# instances usually have JSON disabled or rate-limit shared traffic, which
# is exactly what we hit. Running your own means no rate limit from other
# users and a format we control.
#
# Render deploys this as a separate "Web Service" using "Docker" as the
# environment — see searxng/RENDER_SETUP.md for exact steps.

FROM docker.io/searxng/searxng:latest

# Bake our custom settings.yml (JSON format enabled, limiter relaxed for
# single-user use) into the image so no volume mount is needed on Render.
COPY settings.yml /etc/searxng/settings.yml

# Render injects its own $PORT at runtime; SearXNG's default is 8080.
# We bind to $PORT so Render's health check and routing work correctly.
ENV BIND_ADDRESS=0.0.0.0:8080
EXPOSE 8080
