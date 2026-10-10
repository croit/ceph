# Zone2 connectivity investigation — October 9, 2026

## Verified finding

Fresh TCP connections from `172.31.139.21` to `172.31.139.22` fail selectively
by flow, before SSH authentication. Both SSH port 22 and RGW port 8000 are
affected. An interactive connection can succeed while the next harness
connection times out.

Paired AF_PACKET captures on each node's `eth0` examined only TCP handshake
headers, not application payloads. Eight concurrent four-second connection
probes produced these results:

| Zone1 source port | Zone2 destination port | Result |
|---|---:|---|
| 42022, 42023 | 22 | SYNs and retransmissions visible leaving Zone1, absent at Zone2; timeout |
| 42024, 42025 | 22 | SYN received at Zone2 and SYN-ACK returned; connected |
| 42026, 42027 | 8000 | SYNs and retransmissions visible leaving Zone1, absent at Zone2; timeout |
| 42028, 42029 | 8000 | SYN received at Zone2 and SYN-ACK returned; connected |

Both directions' routes and neighbor MACs matched the intended nodes. Zone2's
sshd and RGW were listening, its container was running with host networking,
and its NVMe workspace was mounted read-write. Both live owned cluster identities
were verified. Changing SSH IPQoS did not eliminate connection failures.

This locates the observed loss between the two guest-interface capture points.
A virtual-switch/VLAN/underlay forwarding or hashed-link fault is a candidate,
not a verified specific device/root cause. No hypervisor or switch configuration
was inspected or changed. No host keys, firewall rules, NIC settings, or cluster
data were changed to bypass this.

## Administrative workaround

An authenticated persistent OpenSSH control connection was established at
`/tmp/opencode/zone2-ssh.sock`. The harness now optionally accepts the absolute
socket path through `RGW_REPRO_SSH_CONTROL_PATH`. Three consecutive remote
commands and both live cluster identity checks succeeded using it.

This reuses an existing transport without retrying/replaying a command or
changing saved lab topology. It does **not** repair native RGW/S3 traffic.
The connection expires after its idle timeout; it is not a permanent host setup.

## Requested no-copy/no-rename test

Only this regression was attempted, using the existing patched two-zone lab:

```bash
RGW_REPRO_SSH_CONTROL_PATH=/tmp/opencode/zone2-ssh.sock .venv-rgw-repro/bin/python ../qa/workunits/rgw/multisite_migration_repro/suspended_empty.py --objects 3 --timeout 90
```

The focused test's constructor call was corrected to supply `backlog=False`.
Python compilation checks and the required `ninja -j10` build check passed;
no other regression/unit-test suite was run.

Run **`rgwlab-20261009-201349-672b`** progressed beyond SSH validation but stopped
before completing initial fixture preparation. Zone1 created the new bucket and
enabled versioning; the bucket appeared in Zone2, but versioning remained `off`.
The owned bucket has the same ID on both zones. Backend bucket stats at
approximately 20:15:58 UTC confirmed `enabled` on Zone1 versus `off` on Zone2.

The report records `completed: false` and:
`Timed out waiting for zone2 versioning=Enabled (last S3 error: None)`.
It is an **incomplete test (exit 2), not a deletion/synchronization verdict**.
No object fixture writes or emptying DELETEs were sent, and no copy, rename,
manual synchronization, purge, or Zone2 cleanup was performed. The report's
generic inherited `method` string mentions CopyObject because preparation
aborted before the focused case initialized its report; no copy actually ran.

Report: `build/rgw-repro/runs/rgwlab-20261009-201349-672b/report.json`.
Bucket: `rgwlab-20261009-201349-672b-suspended-empty-no-rename`.

The flow-selective network fault must be repaired or otherwise isolated before
a clean no-copy/no-rename multisite emptying result can be claimed. Preserve
this incomplete fixture and use a fresh run after network recovery.
