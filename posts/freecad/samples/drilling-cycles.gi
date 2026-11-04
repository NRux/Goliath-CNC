;GOLIATH-GI 1
;GOLIATH SOURCE cam="FreeCAD (stand-in objects, not a FreeCAD run)" post="goliath_post.py 0.1.0"
;GOLIATH UNITS mm
;GOLIATH STOCK x=1220.000 y=610.000 t=18.000 origin=t1-corner zzero=top
;GOLIATH TOWER2 axis=X
;GOLIATH TOOL n=3 d=5.000 flute=25.000 kind=flat rpm=12000 name="5 mm endmill"
;GOLIATH ZSHIFT -19.050
G17 G21 G90
T3 M6
;GOLIATH OP id=Drilling kind=drill through=1 tool=3 label="Through holes"
M3 S12000
; FreeCAD: Drilling
; FreeCAD: Begin Drilling
G90
G0 Z6.000
G0 X100.000 Y100.000
G0 Z4.000
; Note: FreeCAD G81 cycle at X100.000 Y100.000 (bottom Z-18.500, R2.000, G98) expanded into moves by the RS274/NGC rules
G0 Z2.000
G1 Z-18.500 F150
G0 Z4.000
G0 X200.000 Y100.000
; Note: FreeCAD G81 cycle at X200.000 Y100.000 (bottom Z-18.500, R2.000, G98) expanded into moves by the RS274/NGC rules
G0 Z2.000
G1 Z-18.500
G0 Z4.000
G0 Z6.000
;GOLIATH OPEND id=Drilling
;GOLIATH OP id=Drilling001 kind=drill through=auto tool=3 label="Blind holes 10 mm"
M8
M3 S12000
; FreeCAD: Drilling001
; FreeCAD: Begin Drilling
G90
G0 Z6.000
G0 X300.000 Y200.000
G0 Z4.000
; Note: FreeCAD G83 cycle at X300.000 Y200.000 (bottom Z-10.000, R2.000, Q4.000, G99) expanded into moves by the RS274/NGC rules; pecks back off 0.254 mm
G0 Z2.000
G1 Z-2.000 F150
G0 Z2.000
G0 Z-1.746
G1 Z-6.000
G0 Z2.000
G0 Z-5.746
G1 Z-10.000
G0 Z2.000
G0 X350.000 Y200.000
; Note: FreeCAD G83 cycle at X350.000 Y200.000 (bottom Z-10.000, R2.000, Q4.000, G99) expanded into moves by the RS274/NGC rules; pecks back off 0.254 mm
G1 Z-2.000
G0 Z2.000
G0 Z-1.746
G1 Z-6.000
G0 Z2.000
G0 Z-5.746
G1 Z-10.000
G0 Z2.000
G0 Z6.000
M9
;GOLIATH OPEND id=Drilling001
M5
M30
