# Real QEMU media matrix

This package keeps the media integration gates isolated from the shared
integration package. The tests use only prebuilt EFI Shell, OVMF, and CirrOS
inputs; no guest or EFI compiler toolchain is required.

The gates cover:

- CirrOS direct-kernel boot reading a sentinel through the generated ISO9660
  image and observing rejected writes through the SCSI CD-ROM device.
- EFI Shell reading unique FAT sentinels through USB mass storage and NVMe.
- Separate EFI Shell boots observing rejected writes to read-only USB and
  NVMe media.
- A real boot with blank `qemu_scratch_disk` devices attached through USB and
  NVMe. Ordered serial assertions require the OVMF map entries for both blank
  block devices before accepting the file-backed media markers.
- gzip materialization for the seed, ISO, and USB media, plus uncompressed
  materialization for the NVMe medium.

CirrOS 0.6.3's published x86_64 initramfs does not contain `xhci_pci`,
`usb_storage`, or `nvme` kernel modules. Consequently, Linux is used for the
CD-ROM assertion while OVMF/EFI Shell supplies the USB and NVMe guest-side
assertions. The blank USB/NVMe scratch disks are enumerated as block devices,
but are not formatted or written; doing that honestly would require adding
guest filesystem tooling or a larger pinned Linux guest.
