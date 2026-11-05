;GOLIATH-GI 1
;GOLIATH SOURCE cam="red-team suite" post="hand-written 1"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
G0 Z10.000
M3 S18000
;GOLIATH OP id=a kind=engrave through=0 tool=1 c=0
G0 X1300.000 Y600.000
G1 Z-3.000 F200
G1 X1200.000 Y600.000 F800
G0 Z10.000
;GOLIATH OPEND id=a
;GOLIATH OP id=b kind=engrave through=0 tool=1 c=60
G0 X1250.000 Y410.000
G1 Z-3.000 F200
G1 X1301.962 Y380.000 F800
G0 Z10.000
;GOLIATH OPEND id=b
M5
M30
; Licence: SPDX-License-Identifier: GPL-3.0-or-later
