# Running claude-dev under Firecracker

An assessment of whether the current two-container setup can be moved onto
[Firecracker](https://firecracker-microvm.github.io/) to replace the container boundary
with a VM boundary.

**Verdict:** no, not without giving up the host-directory mounts. Firecracker's device
model has no filesystem passthrough and this is a deliberate, documented non-goal rather
than a gap waiting to be filled. The VT console works, but only over SSH — the serial
console cannot carry a resizable TUI. Egress and throwaway sessions are fine. If the goal
is a VM boundary while keeping `-v`-style mounts, see
[cloud-hypervisor-assessment.md](cloud-hypervisor-assessment.md); Cloud Hypervisor shares
Firecracker's rust-vmm lineage and isolation properties but does support virtio-fs.

## Host prerequisites

Identical to the Cloud Hypervisor route: `/dev/kvm` on the WSL host, nested
virtualisation enabled, `vhost_vsock` loaded. This machine reports `vmx` and
`kvm_intel.nested=Y`, so nested virt is on; `/dev/kvm` is not visible from inside the dev
container because the device is not passed in, which is expected.

Firecracker additionally wants membership in the `kvm` group, and `jailer` — the
recommended production wrapper, which chroots and applies its own seccomp and cgroup
policy — wants root to set itself up.

## Requirement by requirement

### Host-directory mounts — not supported

Firecracker's entire device model is `virtio-block`, `virtio-net`, `virtio-vsock`,
`virtio-balloon`, `virtio-rng`, a 16550A serial port and a minimal i8042 for reset.
There is no `virtio-fs`, no 9p and no `vhost-user-fs`. Minimising the device model *is*
the security argument for Firecracker, so this will not change.

That removes the direct equivalent of every `-v` in `start.sh` — the working directory,
the extra `paths` mounts, `~/.claude` and `~/.gitconfig:ro`. What remains:

- **An ext4 image attached as a block device.** No live sharing; the workspace would have
  to be copied in before the session and out afterwards. This defeats the point of the
  workflow and puts the host copy at risk of being stale or clobbered.
- **NFS from the host over a tap device.** Real live sharing, uid mapping handled by NFS.
  But git over NFS is slow on large trees, and it requires the guest to hold an IP route
  back to the host — which undoes the containment gained by the vsock-only egress design
  described in the Cloud Hypervisor assessment.
- **Cloud Hypervisor instead.** `virtio-fs` via an external `virtiofsd`, with
  `--translate-uid` for the unprivileged case. This is the only option that keeps the
  current mount semantics intact.

### VT console — works over SSH, not over the serial console

Firecracker wires guest `ttyS0` to its own stdin/stdout. Claude Code's TUI will render
there, but two properties make it unsuitable as the session console:

- **No terminal size.** A serial line has no in-band `TIOCGWINSZ` channel, so there is no
  `SIGWINCH` to deliver. The guest sees 80x24 unless `stty rows/cols` is set by hand, and
  a resize of the host terminal never propagates. Firecracker has no equivalent of Cloud
  Hypervisor's virtio-console SIGWINCH handler, because it has no virtio-console.
- **Throughput and blocking.** 16550A emulation is byte-oriented, which makes a
  redraw-heavy full-screen TUI sluggish. Firecracker's own documentation treats the
  serial console as a debugging aid and warns that a consumer which does not drain the
  output can stall the vCPU.

The workable path is `sshd` in the guest, reached over vsock or a tap device, with
`ssh -t`. That yields a real pty: `SIGWINCH`, correct `TERM`/`COLORTERM` propagation via
`SendEnv`/`SetEnv`, 256-colour and truecolour, bracketed paste. It also means the guest
carries an SSH daemon and a host key — more moving parts than the current `docker run -it`.

### Proxied egress — works, and comes out stronger

Same conclusion and same design as the Cloud Hypervisor assessment: give the VM no
network device and tunnel to tinyproxy over vsock, with `socat` on both ends and
`HTTP_PROXY=http://127.0.0.1:3128` in the guest. Firecracker's hybrid vsock uses the same
unix-socket scheme — `CONNECT <port>` handshake host-to-guest, a `<socket>_<port>`
listener guest-to-host — so the recipe carries over unchanged.

The `claude-internal` / `claude-egress` split and the WSL DNS wart documented in the
README both disappear, because the guest never resolves anything itself.

The caveat is the console: with no NIC there is no SSH either, which reintroduces the
serial-console problem above. Keeping SSH means adding a tap device and constraining it
in host netfilter — DROP everything from that tap except the proxy address and port —
which is roughly equivalent to today's internal network, enforced by the host firewall
instead of the container engine.

### Throwaway sessions — better than `--rm`

A read-only rootfs drive plus an overlay on tmpfs, or a `cp --reflink=auto` of the rootfs
per start. Enforced at the block layer rather than by `docker --rm`. Firecracker
snapshots could also cut boot time to a fraction of a second for repeated starts, which
is the one place it is genuinely nicer than the alternatives.

### Hardening that becomes unnecessary

As with any VM boundary: the setuid-bit stripping, `rm -f /bin/su …`,
`no-new-privileges` and `--cap-drop NET_ADMIN/NET_RAW` in `claude/Dockerfile` exist
because container root is host root. Guest root is harmless, so those layers can go.

## What the repo would become

- **`build.sh`** keeps the Dockerfile as the source of truth and adds a rootfs stage:
  `docker export` → `mke2fs -d` → `rootfs.img`, plus `openssh-server` if SSH is the
  console. Firecracker accepts **only an uncompressed ELF `vmlinux`** — not a bzImage —
  so a guest kernel has to be built or taken from the Firecracker CI kernel artifacts.
  The WSL2 kernel is not usable as a guest image.
- **A guest `/init`** to mount the pseudo-filesystems, set up the overlay, start the
  socat forwarder and either exec claude on `ttyS0` or start `sshd`.
- **`start.sh`** loses the container networking and gains VM lifecycle management: a
  firecracker API socket, a machine config, drive and vsock configuration, tap setup and
  teardown if SSH is used, and reaping on exit.
- **`start.ps1`** has no path at all — Firecracker is Linux/KVM only, so the Windows
  entry point would have to shell in via `wsl -e`, and the two scripts stop being
  symmetric.

## Recommendation

Firecracker is the wrong shape for this repo. Its design centre is short-lived,
self-contained, network-served workloads — serverless functions — where no host directory
is shared and no interactive terminal is attached. This setup is the opposite on both
counts.

The motivation is sound: the README already names kernel exploits as the residual risk,
and a VM boundary is what buys that down. But the vehicle should be Cloud Hypervisor, or
Kata Containers on top of it — see
[cloud-hypervisor-assessment.md](cloud-hypervisor-assessment.md).

## Sources

- [Firecracker design document — device model](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md)
- [Firecracker FAQ — supported devices, absence of filesystem passthrough](https://github.com/firecracker-microvm/firecracker/blob/main/FAQ.md)
- [Firecracker serial console documentation](https://github.com/firecracker-microvm/firecracker/blob/main/docs/api_requests/actions.md)
- [Firecracker vsock documentation — hybrid vsock and the CONNECT handshake](https://github.com/firecracker-microvm/firecracker/blob/main/docs/vsock.md)
- [Firecracker jailer documentation](https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md)
