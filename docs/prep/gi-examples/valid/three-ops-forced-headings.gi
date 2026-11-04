;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT -18.000
; Part A uses an S-shaped lead-in. This comment starts with P, so it is a plain comment.
;GOLIATH OP id=pocket-1 kind=pocket through=0 tool=1 label="Tasche für Scharnier"
G0 Z10.000
G0 X600.000 Y400.000
M3 S18000
G0 Z5.000
G1 Z-4.000 F200
G1 X700.000 F800
G1 Y450.000
G1 X600.000
G1 Y400.000
G0 Z10.000
;GOLIATH OPEND id=pocket-1
;GOLIATH OP id=outline-2 kind=profile through=1 tool=1 label="Teil Ä — Außenkontur" c=90
G0 X800.000 Y300.000
G0 Z5.000 
G1 Z-18.200 F200
G1 X1100.000 F800
G1 Y600.000
G1 X800.000
G1 Y300.000
G0 Z10.000  
;GOLIATH OPEND id=outline-2
;GOLIATH OP id=hole.3 kind=drill through=auto tool=1 c=360
G0 X1500.000 Y500.000
G0 Z5.000
G1 Z-10.000 F150
G0 Z10.000
;GOLIATH OPEND id=hole.3
M5
M30
; Licence: SPDX-License-Identifier: Apache-2.0
