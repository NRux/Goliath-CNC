;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=bottom
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH OP id=op1 kind=profile through=auto tool=1 label="Part A outline"
G0 Z28.000
G0 X1000.000 Y500.000
M3 S18000
G0 Z23.000
G1 Z12.000 F200
G1 X1200.000 F800
G0 Z28.000
;GOLIATH OPEND id=op1
M5
; Licence: SPDX-License-Identifier: Apache-2.0
