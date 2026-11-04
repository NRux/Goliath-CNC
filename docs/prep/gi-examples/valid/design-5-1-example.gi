;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT -18.000
;GOLIATH OP id=op3 kind=profile through=auto tool=1 label="Part A outline"
G0 X100.000 Y200.000
G0 Z5.000
M3 S18000
G1 Z-18.500 F200
G1 X300.000 F800
G2 X350.000 Y250.000 I0.000 J50.000
G0 Z10.000
;GOLIATH OPEND id=op3
; Licence: SPDX-License-Identifier: Apache-2.0
