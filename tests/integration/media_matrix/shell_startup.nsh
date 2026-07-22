@echo -off
connect -r
map -r
echo OSTEST: MEDIA SHELL

if exist fs0:\USB_SENTINEL.txt then
  set usbfs fs0:
  goto usb-found
endif
if exist fs1:\USB_SENTINEL.txt then
  set usbfs fs1:
  goto usb-found
endif
if exist fs2:\USB_SENTINEL.txt then
  set usbfs fs2:
  goto usb-found
endif
if exist fs3:\USB_SENTINEL.txt then
  set usbfs fs3:
  goto usb-found
endif
if exist fs4:\USB_SENTINEL.txt then
  set usbfs fs4:
  goto usb-found
endif
if exist fs5:\USB_SENTINEL.txt then
  set usbfs fs5:
  goto usb-found
endif
if exist fs6:\USB_SENTINEL.txt then
  set usbfs fs6:
  goto usb-found
endif
if exist fs7:\USB_SENTINEL.txt then
  set usbfs fs7:
  goto usb-found
endif
echo OSTEST: FAIL USB MEDIA SENTINEL
goto done

:usb-found
type %usbfs%\USB_SENTINEL.txt
echo OSTEST: USB MEDIA SENTINEL

if exist fs0:\NVME_SENTINEL.txt then
  set nvmefs fs0:
  goto nvme-found
endif
if exist fs1:\NVME_SENTINEL.txt then
  set nvmefs fs1:
  goto nvme-found
endif
if exist fs2:\NVME_SENTINEL.txt then
  set nvmefs fs2:
  goto nvme-found
endif
if exist fs3:\NVME_SENTINEL.txt then
  set nvmefs fs3:
  goto nvme-found
endif
if exist fs4:\NVME_SENTINEL.txt then
  set nvmefs fs4:
  goto nvme-found
endif
if exist fs5:\NVME_SENTINEL.txt then
  set nvmefs fs5:
  goto nvme-found
endif
if exist fs6:\NVME_SENTINEL.txt then
  set nvmefs fs6:
  goto nvme-found
endif
if exist fs7:\NVME_SENTINEL.txt then
  set nvmefs fs7:
  goto nvme-found
endif
echo OSTEST: FAIL NVME MEDIA SENTINEL
goto done

:nvme-found
type %nvmefs%\NVME_SENTINEL.txt
echo OSTEST: NVME MEDIA SENTINEL
echo OSTEST: MEDIA SHELL PASS

:done
