FROM debian:13-slim

ARG NODE_MAJOR=22
ARG DOTNET_CHANNEL=10.0
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        gnupg \
        git \
        git-lfs \
        openssh-client \
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
        pkg-config \
        python3 \
        python3-venv \
        python3-pip \
        libssl3 \
        libicu-dev \
        libkrb5-3 \
        libgssapi-krb5-2 \
        libstdc++6 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

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

# more deps for downloaded dev-tools
RUN npx playwright install-deps

# No path to root from inside: every setuid/setgid bit is cleared and the
# switch-user tools are removed, so the container user stays the container user.
RUN find / -xdev -perm /6000 -type f -exec chmod ug-s {} + \
    && rm -f /bin/su /usr/bin/newgrp /usr/bin/chsh /usr/bin/chfn /usr/bin/gpasswd

USER ${USERNAME}
ENV HOME=/home/${USERNAME} \
    NPM_CONFIG_PREFIX=/home/${USERNAME}/.npm-global \
    NODE_OPTIONS=--max-old-space-size=4096
ENV PATH=${HOME}/.npm-global/bin:${HOME}/.dotnet/tools:${PATH}

RUN mkdir -p "${NPM_CONFIG_PREFIX}" "${HOME}/.claude" \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && claude --version

WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
