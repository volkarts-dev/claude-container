FROM debian:13-slim

ARG NODE_MAJOR=22
ARG DOTNET_CHANNEL=10.0
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive

# C/C++ tooling comes from the distro too: gcc and clang, clang-format (with
# git-clang-format and clang-format-diff), the clang compiler-rt runtimes and the
# GCC sanitizer runtimes (ASan, UBSan, plus LSan, TSan, HWASan where gcc has them)
RUN set -eux; \
    SANITIZERS="libasan8 libubsan1"; \
    case "$(dpkg --print-architecture)" in \
        amd64|arm64) SANITIZERS="$SANITIZERS liblsan0 libtsan2 libhwasan0" ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        gnupg \
        git \
        git-lfs \
        openssh-client \
        socat \
        less \
        jq \
        ripgrep \
        fd-find \
        unzip \
        zip \
        xz-utils \
        tar \
        procps \
        psmisc \
        htop \
        vim \
        nano \
        locales \
        ncurses-term \
        tini \
        man-db \
        build-essential \
        clang \
        clang-format \
        clang-tidy \
        libclang-rt-dev \
        pkg-config \
        cmake \
        ninja-build \
        glslc \
        libx11-dev \
        libxext-dev \
        libxrandr-dev \
        libxinerama-dev \
        libxcursor-dev \
        libxi-dev \
        libxkbcommon-dev \
        libwayland-dev \
        libgl-dev \
        xvfb \
        xauth \
        python3 \
        python3-venv \
        python3-pip \
        librsvg2-bin \
        imagemagick \
        fonts-dejavu \
        fonts-noto-core \
        fonts-inter \
        fonts-jetbrains-mono \
        libssl3 \
        libicu-dev \
        libkrb5-3 \
        libgssapi-krb5-2 \
        libstdc++6 \
        zlib1g \
        $SANITIZERS; \
    rm -rf /var/lib/apt/lists/*

# Locale and timezone: en_US.UTF-8 is generated here (the base image only has C.UTF-8),
# and the zone is set both as /etc/localtime and as TZ, since not every runtime reads both
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TZ=Europe/Berlin
RUN set -eux; \
    sed -i 's/^# *\(en_US.UTF-8 UTF-8\)$/\1/' /etc/locale.gen; \
    locale-gen; \
    locale -a | grep -qx en_US.utf8; \
    ln -sfn "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    date

# Node.js from the official nodejs.org build (latest v${NODE_MAJOR}.x), checksum verified
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) NODE_ARCH=x64 ;; \
        arm64) NODE_ARCH=arm64 ;; \
        armhf) NODE_ARCH=armv7l ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    BASE="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"; \
    TARBALL="$(curl -fsSL "$BASE/" | grep -o "node-v${NODE_MAJOR}\.[0-9.]*-linux-${NODE_ARCH}\.tar\.xz" | head -n1)"; \
    curl -fsSLO "$BASE/$TARBALL"; \
    curl -fsSL "$BASE/SHASUMS256.txt" | grep " $TARBALL\$" | sha256sum -c -; \
    tar -xJf "$TARBALL" -C /usr/local --strip-components=1 --no-same-owner \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm "$TARBALL"; \
    node --version; npm --version

# .NET SDK via the official install script
ENV DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
RUN set -eux; \
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh; \
    chmod +x /tmp/dotnet-install.sh; \
    /tmp/dotnet-install.sh --channel "${DOTNET_CHANNEL}" --install-dir "${DOTNET_ROOT}"; \
    rm /tmp/dotnet-install.sh; \
    ln -s "${DOTNET_ROOT}/dotnet" /usr/local/bin/dotnet; \
    dotnet --info

# PowerShell from the official GitHub release tarball, checksum verified
ENV PWSH_ROOT=/opt/microsoft/powershell/7 \
    POWERSHELL_TELEMETRY_OPTOUT=1 \
    POWERSHELL_UPDATECHECK=Off
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) PWSH_ARCH=x64 ;; \
        arm64) PWSH_ARCH=arm64 ;; \
        armhf) PWSH_ARCH=arm32 ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    TAG="$(curl -fsSL https://api.github.com/repos/PowerShell/PowerShell/releases/latest | jq -r .tag_name)"; \
    VERSION="${TAG#v}"; \
    TARBALL="powershell-${VERSION}-linux-${PWSH_ARCH}.tar.gz"; \
    BASE="https://github.com/PowerShell/PowerShell/releases/download/${TAG}"; \
    curl -fsSLO "$BASE/$TARBALL"; \
    curl -fsSL "$BASE/hashes.sha256" | iconv -f UTF-16 -t UTF-8 | tr -d '\r' \
        | grep " \*$TARBALL\$" | sha256sum -c -; \
    mkdir -p "${PWSH_ROOT}"; \
    tar -xzf "$TARBALL" -C "${PWSH_ROOT}" --no-same-owner; \
    rm "$TARBALL"; \
    chmod +x "${PWSH_ROOT}/pwsh"; \
    ln -s "${PWSH_ROOT}/pwsh" /usr/local/bin/pwsh; \
    pwsh --version

RUN if getent group "${USER_GID}" >/dev/null; then \
        groupmod -n "${USERNAME}" "$(getent group "${USER_GID}" | cut -d: -f1)"; \
    else \
        groupadd -g "${USER_GID}" "${USERNAME}"; \
    fi \
    && if getent passwd "${USER_UID}" >/dev/null; then \
        usermod -l "${USERNAME}" -d "/home/${USERNAME}" -m -g "${USER_GID}" "$(getent passwd "${USER_UID}" | cut -d: -f1)"; \
    else \
        useradd -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/bash "${USERNAME}"; \
    fi \
    && mkdir -p /workspace && chown "${USER_UID}:${USER_GID}" /workspace

# Playwright with Chromium and its system dependencies in one go. It is installed
# as root (deps need apt), so the browsers go to a shared path the user owns and
# can add other browsers or revisions to.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN set -eux; \
    npm install -g playwright; \
    playwright install --with-deps chromium; \
    chown -R "${USER_UID}:${USER_GID}" "${PLAYWRIGHT_BROWSERS_PATH}"; \
    npm cache clean --force; \
    rm -rf /var/lib/apt/lists/*; \
    playwright --version

# The entrypoint starts Xvfb as the container user, which can't create the X
# socket directory itself the way the X server expects it (root-owned, sticky)
RUN install -d -m 1777 /tmp/.X11-unix
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# No path to root from inside: every setuid/setgid bit is cleared and the
# switch-user tools are removed, so the container user stays the container user.
RUN find / -xdev -perm /6000 -type f -exec chmod ug-s {} + \
    && rm -f /bin/su /usr/bin/newgrp /usr/bin/chsh /usr/bin/chfn /usr/bin/gpasswd

# and now the user stuff
USER ${USERNAME}

ENV HOME=/home/${USERNAME} \
    NPM_CONFIG_PREFIX=/home/${USERNAME}/.npm-global \
    NODE_OPTIONS=--max-old-space-size=4096
ENV PATH=${HOME}/.npm-global/bin:${HOME}/.dotnet/tools:${PATH}

RUN mkdir -p "${NPM_CONFIG_PREFIX}" "${HOME}/.claude" \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && claude --version

# disabled updates (they would be gone on container restart anyway)
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1

# default startup values
ENV DISPLAY=:99
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
