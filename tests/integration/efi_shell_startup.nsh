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
  setvar RulesOstestPersistence -guid 98b01f10-1f13-4bd2-9173-fad1c483cc19
  if %lasterror% == 0 then
    echo OSTEST: VARIABLE READ COMMAND OK
    echo OSTEST: PASS
  else
    echo OSTEST: FAIL VARIABLE READ
  endif
else
  setvar RulesOstestPersistence -guid 98b01f10-1f13-4bd2-9173-fad1c483cc19 -nv -bs -rt =4F53544553542D5641522D504552534953544544
  if %lasterror% == 0 then
    echo OSTEST: VARIABLE WRITE COMMAND OK
    echo phase-one > fs0:\EFI\OSTEST\phase-two.flag
    if exist fs0:\EFI\OSTEST\phase-two.flag then
      echo OSTEST: PHASE ONE WRITE
      reset -c
    else
      echo OSTEST: FAIL WRITE
    endif
  else
    echo OSTEST: FAIL VARIABLE WRITE
  endif
endif

:done
