# SPDX-License-Identifier: GPL-3.0-or-later
# Image for `python tools/local_ci.py --where docker`: Debian with git and uv.
# uv then downloads each Python version the CI jobs use into the cache volume
# goliath-local-ci-cache, so each is fetched once.
#
# The build needs no TLS: git comes from Debian's signed package archive and uv
# is copied from its official image, so it also works behind a proxy that
# re-signs HTTPS. Both images are pinned by digest; to move one on, replace its
# digest with the one `docker pull <image>` reports. The image tag that
# local_ci.py uses is a hash of this file, so an edit builds a new image.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
