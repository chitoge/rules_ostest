@echo -off
map -r
if exist fs0:\EFI\OSTEST\fixture.txt then
  echo OSTEST: EFI SHELL
else
  echo OSTEST: FAIL MISSING FIXTURE
  goto done
endif

if exist fs0:\EFI\OSTEST\phase-two.flag then
  echo OSTEST: PHASE TWO READ
  echo OSTEST: PASS
else
  echo phase-one > fs0:\EFI\OSTEST\phase-two.flag
  if exist fs0:\EFI\OSTEST\phase-two.flag then
    echo OSTEST: PHASE ONE WRITE
    reset -c
  else
    echo OSTEST: FAIL WRITE
  endif
endif

:done
