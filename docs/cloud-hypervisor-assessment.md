# Running claude-dev under Cloud Hypervisor

An assessment of whether the current two-container setup can be moved onto
[Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor) to replace the
container boundary with a VM boundary, and what it would cost.

**Verdict:** every capability this repo depends on — host-directory mounts, a full VT
console, proxied egress, throwaway sessions — is supported. But Cloud Hypervisor is a
VMM, not a container engine. `start.sh` goes from wiring up two `docker run` calls to
building a guest kernel and rootfs, launching one virtiofsd per mount, bridging a
socket, and booting a VM. See [The shortcut worth considering](#the-shortcut-worth-considering)
before committing to it.

## Host prerequisites

The WSL2 kernel on this machine (6.18.33.2-microsoft-standard-WSL2) is built to be both
a KVM host and a virtio-fs/vsock guest:

```
CONFIG_KVM=m   CONFIG_VHOST_VSOCK=m   CONFIG_VIRTIO_FS=y   CONFIG_VIRTIO_CONSOLE=y
cpuinfo flags: vmx, ept, hypervisor
```

Nested virtualisation is evidently enabled. Confirm on the WSL host — not inside the dev
container, which is not given the device:

```sh
ls -l /dev/kvm
sudo modprobe vhost_vsock
```

## Requirement by requirement

### Host-directory mounts — works, with friction

`--fs tag=…,socket=…`, backed by an external `virtiofsd` process. `--memory shared=on` is
mandatory for it to function at all.

- **One virtiofsd process per tag.** `start.sh` mounts the working directory plus
  arbitrary extra paths (`assign_name`, the `-v` loop). Each becomes its own daemon,
  socket, `--fs` device and `mount -t virtiofs` in the guest init. Symlinking the paths
  into a single shared directory does not work — virtiofsd will not follow links out of
  the share.
- **`~/.gitconfig:ro` has no equivalent.** virtio-fs shares directories, not single
  files. Copy it into the ephemeral overlay at boot instead.
- **uid/gid.** Running virtiofsd as the invoking user (the rootless-podman-equivalent
  case), it can only create files under its own uid, so `--translate-uid` /
  `--translate-gid` are needed to map guest uid 1000 onto the host uid. These cannot be
  combined with `--posix-acl`. The existing "build the image with the host's uid/gid"
  approach carries over unchanged.
- DAX is still not considered stable and is unavailable in Cloud Hypervisor. Guest
  kernel 5.10+ required.

### VT console — works

`--console tty` provides virtio-console (`hvc0`). Cloud Hypervisor installs a **SIGWINCH**
handler to resize the guest console; the previously separate tty and pty code paths were
unified into one handler. Resize, raw-mode Ctrl-C and 24-bit colour all survive.

Two gaps:

- `TERM`, `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION` have no `-e` equivalent to
  ride in on. Pass them on the kernel command line, or drop a generated env file into one
  of the shared directories.
- There is no QEMU-style `Ctrl-A x` escape. The VM is stopped through its API socket.

### Proxied egress — works, and comes out stronger

The cleanest design gives the VM **no network device at all** and tunnels the proxy over
vsock:

```sh
# guest
socat TCP-LISTEN:3128,fork VSOCK-CONNECT:2:3128
# HTTP_PROXY=http://127.0.0.1:3128

# host
socat UNIX-LISTEN:/tmp/ch.vsock_3128,fork TCP:127.0.0.1:${PROXY_PORT}
```

Cloud Hypervisor's hybrid vsock needs the `CONNECT <port>` handshake only in the
host-to-guest direction; guest-to-host is plain socat against the `<socket>_<port>` unix
listener. `PROXY_PORT` and `PROXY_BIND` already exist in `start.sh` for exactly this.

The tinyproxy container stays as it is. DNS stops being a concern entirely, because the
proxy `CONNECT` carries hostnames — which removes the whole `claude-internal` /
`claude-egress` two-network arrangement and the WSL DNS wart documented in the README.

Note that anything not proxy-aware fails outright rather than merely failing to route,
and `ping`/`dig` diagnostics disappear. Neither is a regression, but it changes how
network problems present.

### Throwaway sessions — better than `--rm`

Boot `--disk path=rootfs.img,readonly=on` with an overlayfs on tmpfs in the guest.
Nothing written outside the virtio-fs mounts can persist, enforced by the block layer
rather than by `docker --rm`.

### Hardening that becomes unnecessary

The setuid-bit stripping, `rm -f /bin/su …`, `no-new-privileges` and
`--cap-drop NET_ADMIN/NET_RAW` all exist because container root is host root. Behind a VM
boundary guest root is harmless, so `claude/Dockerfile` simplifies noticeably.

## What the repo would become

- **`build.sh`** keeps the Dockerfile as the source of truth and adds a stage:
  `docker export` → `mke2fs -d` → `rootfs.img`. Plus a guest kernel (a distro `vmlinux`
  or bzImage; Cloud Hypervisor takes either), or `rust-hypervisor-firmware` to boot a
  conventional cloud image instead.
- **A guest `/init`** that mounts the pseudo-filesystems and the virtio-fs tags, sets up
  the overlay, starts the socat forwarder, and `exec`s claude on `hvc0`.
- **`start.sh`** loses the network plumbing and gains virtiofsd lifecycle management —
  spawn per mount, wait for sockets, reap on exit.

## The shortcut worth considering

**Kata Containers with the Cloud Hypervisor backend** (`configuration-clh.toml`) gives
the same VM boundary while leaving `start.sh` nearly untouched: Kata translates `-v` into
virtio-fs and container networks into a tap in the network namespace, so the
internal/egress split and the mount-naming logic survive verbatim.

The open question is runtime integration. Kata 2.x and later target the containerd
shim-v2 interface, and it is unclear whether podman drives that cleanly today —
Docker-on-containerd is the well-trodden path. Worth verifying before hand-rolling
Cloud Hypervisor.

`krunvm` / libkrun is a third option in the same spirit: an OCI image straight into a
microVM with virtio-fs and vsock.

Both still need `/dev/kvm`, so the WSL prerequisites above apply either way.

## Recommendation

The Cloud Hypervisor route is a real project, not a weekend change. The isolation gain
over the current setup is meaningful only against kernel exploits — which the README
already names as the residual risk. If that is the threat being bought down, it is worth
doing; try Kata first.

## Sources

- [Cloud Hypervisor virtio-fs documentation](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/fs.md)
- [Cloud Hypervisor vsock documentation](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/vsock.md)
- [Cloud Hypervisor v20.2 release notes — console resize / SIGWINCH](https://www.cloudhypervisor.org/blog/cloud-hypervisor-v20.2-released/)
- [virtiofsd documentation — `--translate-uid`, unprivileged operation](https://docs.rs/crate/virtiofsd/latest)
