;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD 1.1.4" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=2440.000 y=1220.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=1 d=6.000 flute=22.000 stickout=40.000 kind=flat rpm=18000 name="6mm 2F upcut"
;GOLIATH ZSHIFT -18.000
;GOLIATH OP id=op1 kind=profile through=auto tool=1 label="Part A outline"
G00Z10
G0X1000Y500
M03S18000
G0 Z+5
G1Z-6.0F200
G1 X1200.1234 F800.5
G1	Y700
G0 Z10
;GOLIATH OPEND id=op1
M05
; Licence: SPDX-License-Identifier: Apache-2.0
