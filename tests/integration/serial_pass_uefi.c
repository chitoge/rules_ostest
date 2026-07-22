/*
 * Minimal x86-64 UEFI application for the real-QEMU integration smoke test.
 *
 * It deliberately has no C library or UEFI-library dependency. The public
 * rules place this PE/COFF image at EFI/BOOT/BOOTX64.EFI, and the application
 * writes its verdict directly to the first PC serial port used by the test
 * runner.
 */

typedef void *efi_handle;
typedef struct efi_system_table efi_system_table;
typedef unsigned long long efi_status;

static inline void outb(unsigned short port, unsigned char value) {
    __asm__ volatile("outb %0, %1" : : "a"(value), "Nd"(port));
}

static inline unsigned char inb(unsigned short port) {
    unsigned char value;
    __asm__ volatile("inb %1, %0" : "=a"(value) : "Nd"(port));
    return value;
}

static void serial_putc(char value) {
    while ((inb(0x3fd) & 0x20) == 0) {
    }
    outb(0x3f8, (unsigned char)value);
}

__attribute__((ms_abi))
efi_status efi_main(efi_handle image, efi_system_table *system_table);

/*
 * Keep one image-base relocation in the otherwise position-independent
 * program. OVMF rejects a relocatable EFI application with no .reloc entries.
 */
void *volatile image_relocation = (void *)&efi_main;

__attribute__((ms_abi))
efi_status efi_main(efi_handle image, efi_system_table *system_table) {
    static const char verdict[] =
        "OSTEST: REAL UEFI BOOT\r\n"
        "OSTEST: PASS\r\n";
    unsigned long long index;

    (void)image;
    (void)system_table;
    (void)image_relocation;

    /* Configure COM1 for 115200 baud, 8 data bits, no parity, one stop bit. */
    outb(0x3f9, 0x00);
    outb(0x3fb, 0x80);
    outb(0x3f8, 0x01);
    outb(0x3f9, 0x00);
    outb(0x3fb, 0x03);
    outb(0x3fa, 0xc7);
    outb(0x3fc, 0x0b);

    for (index = 0; index < sizeof(verdict) - 1; ++index) {
        serial_putc(verdict[index]);
    }

    /* The host runner terminates QEMU as soon as it observes the verdict. */
    for (;;) {
        __asm__ volatile("hlt");
    }
}
