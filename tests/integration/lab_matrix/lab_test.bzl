"""Real CirrOS isolated-network lab targets."""

load(
    "//ostest:defs.bzl",
    "qemu_media",
    "qemu_network",
    "uefi_lab_test",
    "uefi_vm",
)

def _cirros_vm(name, seed, mac, qemu):
    return uefi_vm(
        name = name,
        arch = "x86_64",
        boot = "direct-kernel",
        initrd = "@cirros_x86_64_initramfs//file",
        kernel = "@cirros_x86_64_kernel//file",
        kernel_args = "console=ttyS0 root=/dev/vda1 ro",
        media = [
            qemu_media(
                name = "root",
                bootindex = 1,
                compression = "none",
                image = "@cirros_x86_64_disk//file",
                image_format = "qcow2",
                readonly = False,
                snapshot = True,
            ),
            qemu_media(
                name = "seed",
                bootindex = None,
                image = seed,
                readonly = True,
            ),
        ],
        memory_mb = 192,
        networks = [qemu_network(
            name = "lan",
            mac = mac,
            model = "virtio-net-pci",
        )],
        qemu = qemu,
    )

def real_cirros_lab(name, qemu):
    """Defines one two-guest real CirrOS lab under the selected QEMU wrapper."""
    uefi_lab_test(
        name = name,
        size = "medium",
        srcs = ["real_cirros_lab_test.py"],
        main = "real_cirros_lab_test.py",
        tags = [
            "external",
            "manual",
            "no-remote",
        ],
        timeout = "moderate",
        vms = [
            _cirros_vm("server", ":server_seed", "52:54:00:12:34:10", qemu),
            _cirros_vm("client", ":client_seed", "52:54:00:12:34:11", qemu),
        ],
    )
