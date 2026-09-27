; Splitter installer hooks (Tauri 2 NSIS bundler, bundle.windows.nsis.installerHooks).
;
; The installed app runs Splitter as a Windows service from boot (docs/sync-design.md):
; after the files land, the shell's headless --install-service mode unpacks the sidecar
; into C:\ProgramData\Splitter\bin and registers + starts the service; before the files go,
; --remove-service stops and deletes it. Both are idempotent, so an update simply
; re-registers the service against the new sidecar. Output goes to
; C:\ProgramData\Splitter\splitter-desktop.log (the shell has no console).

!macro NSIS_HOOK_PREINSTALL
  ; An update over a running install: stop the old service so nothing is busy.
  ; (Harmless when there is none: --remove-service tolerates a missing service.)
  IfFileExists "$INSTDIR\splitter-desktop.exe" 0 +2
    ExecWait '"$INSTDIR\splitter-desktop.exe" --remove-service' $0
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ExecWait '"$INSTDIR\splitter-desktop.exe" --install-service' $0
  ${If} $0 != 0
    DetailPrint "Splitter: the service could not be registered (exit $0) — see C:\ProgramData\Splitter\splitter-desktop.log"
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ExecWait '"$INSTDIR\splitter-desktop.exe" --remove-service' $0
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; The data dir (database, log, unpacked sidecar) is left in place on purpose:
  ; the runs are the user's. Delete C:\ProgramData\Splitter by hand to wipe them.
!macroend
