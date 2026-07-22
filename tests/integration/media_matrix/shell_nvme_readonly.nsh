@echo -off
map -r
echo OSTEST: NVME READONLY PROBE

if exist fs0:\NVME_SENTINEL.txt then
  set mediafs fs0:
  goto found
endif
if exist fs1:\NVME_SENTINEL.txt then
  set mediafs fs1:
  goto found
endif
if exist fs2:\NVME_SENTINEL.txt then
  set mediafs fs2:
  goto found
endif
if exist fs3:\NVME_SENTINEL.txt then
  set mediafs fs3:
  goto found
endif
echo OSTEST: FAIL NVME MEDIA SENTINEL
goto done

:found
type %mediafs%\NVME_SENTINEL.txt
echo OSTEST: NVME MEDIA SENTINEL
echo forbidden > %mediafs%\OSTEST_WRITE.txt

:done
