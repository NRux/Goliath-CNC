;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD (stand-in objects, not a FreeCAD run)" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=800.000 y=600.000 t=12.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=Y
;GOLIATH TOOL n=2 d=3.000 flute=12.000 kind=ball
;GOLIATH ZSHIFT -12.000
; Note: the tool controllers of tool 2 run at different spindle speeds (16000, 20000 rpm); TOOL rpm is left out and each operation starts the spindle at its own speed
; Note: the tool controllers of tool 2 name their bit differently (3 mm ball, 3 mm ball001); TOOL name is left out
G17 G21 G90
T2 M6
;GOLIATH OP id=Adaptive kind=adaptive through=auto tool=2 label="Adaptive clearing"
M3 S20000
; FreeCAD: Adaptive
G0 Z5.000
G0 X205.000 Y200.000
G0 Z1.000
G1 Z0.000 F200
G3 X195.000 Y200.000 Z-0.500 I-5.000 J0.000
G3 X205.000 Y200.000 Z-1.000 I5.000 J0.000
G3 X205.000 Y200.000 Z-1.000 I-5.000 J0.000 F800
G1 X230.000 Y200.000
G0 Z5.000
;GOLIATH OPEND id=Adaptive
;GOLIATH OP id=Engrave kind=engrave through=auto tool=2 label="Text"
M3 S16000
; FreeCAD: Engrave
G0 Z5.000
G0 X50.000 Y50.000
G1 Z-0.500 F200
G1 X150.000 Y50.000 F600
G1 X150.000 Y80.000
G0 Z5.000
;GOLIATH OPEND id=Engrave
M5
M30
