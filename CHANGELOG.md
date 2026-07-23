# Changelog

Notable user-visible changes are recorded here.

## 0.1.0 - Unreleased

- Initial public release.
- Add deterministic FAT32, GPT, MBR, fixed-layout, and UEFI ISO image rules.
- Add serial-driven, QMP-scripted, graphical, persistent-variable, and
  isolated multi-VM QEMU test support for x86-64 and AArch64 guests.
- Add ordered and forbidden serial markers plus QMP-validated reboot phases.
- Add UEFI and direct-kernel boot profiles, including real-capable AArch64
  `virt` machine and CPU configuration.
- Add managed loopback host forwarding with collision-free automatic ports and
  declared one-shot host companions.
- Add stable serial artifacts, bounded post-failure hooks, guest display
  devices, and non-blank screendump assertions.
- Add explicit KVM run-or-skip policy with skipped JUnit output and uncached
  environment-dependent results.
- Add writable scratch media, safe runfile copying, post-stop readback, and
  exported disk artifacts.
- Preserve requested FAT volume-label casing, including lowercase NoCloud
  `cidata` labels.
- Document Git-only consumption and distinguish local system QEMU, staged
  runfiles, and content-pinned remote-execution bundles.
