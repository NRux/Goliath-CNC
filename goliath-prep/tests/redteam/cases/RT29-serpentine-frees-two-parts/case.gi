;GOLIATH-GI 1
;GOLIATH SOURCE cam="red-team suite" post="hand-written 1"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
G0 Z10.000
M3 S18000
;GOLIATH OP id=serpentine kind=profile through=1 tool=1 c=120
G0 X1170.000 Y320.000
G1 Z-18.300 F200
G1 X1170.000 Y275.000 F800
G1 X1170.000 Y320.000
G1 X1235.000 Y320.000
G1 X1335.000 Y320.000
G0 Z10.000
;GOLIATH OPEND id=serpentine
M5
M30
; Licence: SPDX-License-Identifier: GPL-3.0-or-later
