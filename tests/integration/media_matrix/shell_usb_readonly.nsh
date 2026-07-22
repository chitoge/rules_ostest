@echo -off
connect -r
map -r
echo OSTEST: USB READONLY PROBE

if exist fs0:\USB_SENTINEL.txt then
  set mediafs fs0:
  goto found
endif
if exist fs1:\USB_SENTINEL.txt then
  set mediafs fs1:
  goto found
endif
if exist fs2:\USB_SENTINEL.txt then
  set mediafs fs2:
  goto found
endif
if exist fs3:\USB_SENTINEL.txt then
  set mediafs fs3:
  goto found
endif
echo OSTEST: FAIL USB MEDIA SENTINEL
goto done

:found
type %mediafs%\USB_SENTINEL.txt
echo OSTEST: USB MEDIA SENTINEL
echo forbidden > %mediafs%\OSTEST_WRITE.txt

:done
