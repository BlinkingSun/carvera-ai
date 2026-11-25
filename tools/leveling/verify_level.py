import math, re, sys
orig = open(sys.argv[1],'rb').read().decode().split('\r\n')[:-1]
lev  = open(sys.argv[2],'rb').read().decode().split('\r\n')[:-1]
import json
G = json.load(open(sys.argv[3]))
# independent surface: tensor-product hat functions, edge cells extended linearly
XS=G["xs"]; YS=G["ys"]
Z={(XS[i],YS[j]):G["z"][j][i] for i in range(len(XS)) for j in range(len(YS))}
def hats(v, ax):
    w=[0.0]*len(ax)
    if v <= ax[1]: t=(v-ax[0])/(ax[1]-ax[0]); w[0],w[1]=1-t,t
    elif v >= ax[-2]: t=(v-ax[-2])/(ax[-1]-ax[-2]); w[-2],w[-1]=1-t,t
    else:
        for i in range(len(ax)-1):
            if ax[i] <= v <= ax[i+1]: t=(v-ax[i])/(ax[i+1]-ax[i]); w[i],w[i+1]=1-t,t; break
    return w
def comp(x,y):
    wx,wy=hats(x,XS),hats(y,YS); return sum(wx[i]*wy[j]*Z[(XS[i],YS[j])] for i in range(len(XS)) for j in range(len(YS)))
def parse(s):
    c=re.sub(r"\(.*?\)","",s).strip().upper()
    return c, dict((k,float(v)) for k,v in re.findall(r"([A-Z])(-?\d*\.?\d+)",c))
def is_motion(s):
    c,w=parse(s); return bool(c) and bool({'X','Y','Z'}&w.keys()) and not re.search(r"G28|G53",c)
# 1. non-motion lines identical, in order (leveled file has 3 extra header lines)
nm_o=[l for l in orig if not is_motion(l)]; nm_l=[l for l in lev if not is_motion(l)]
extra=[l for l in nm_l if l.startswith('(OFFLINE') or l.startswith('(grid ') or l=='M370']
nm_l2=[l for l in nm_l if l not in extra]
print("1. non-motion lines identical:", nm_o==nm_l2, f"({len(nm_o)} lines); inserted:", extra)
first_tc=next(i for i,l in enumerate(lev) if re.match(r'T-?\d+M6',l)); assert lev.index('M370') < first_tc, "M370 must precede the first tool change"
# 2. walk both programs
def walk(lines):
    x=y=z=None; mode=0; f=None; out=[]
    for s in lines:
        c,w=parse(s)
        if not c: continue
        if re.search(r"M0?6(?!\d)|T-?\d+M6|G28|G53",c.replace(' ','')): out.append(('reset',)); x=y=z=None; continue
        for g in re.findall(r"G(\d+)",c):
            if int(g) in (0,1,2,3): mode=int(g)
        if not ({'X','Y','Z'}&w.keys()): continue
        if 'F' in w: f=w['F']
        nx,ny,nz=w.get('X',x),w.get('Y',y),w.get('Z',z)
        out.append(('m',mode,x,y,z,nx,ny,nz,w.get('I',0.0),w.get('J',0.0),f,'F' in w))
        x,y,z=nx,ny,nz
    return out
O,L=walk(orig),walk(lev)
li=0; worst=dict(endxy=0,endz=0,line_off=0,arc_rad=0,arc_ctr=0,seg=0); nO=0; f_bad=0
for o in O:
    if o[0]=='reset':
        assert L[li][0]=='reset', f"reset mismatch at L{li}"; li+=1; continue
    nO+=1; _,mo,ox,oy,oz,tx,ty,tz,oi,oj,of,ofs=o
    while True:
        l=L[li]; li+=1; assert l[0]=='m', f"unexpected reset in leveled at {li}"
        _,ml,lx,ly,lz,px,py,pz,li_,lj,lf,lfs=l
        if lf!=of: f_bad+=1
        if mo in (0,1) or ox is None:
            assert ml==mo, f"mode {ml} vs {mo}"
            if ox is not None and (tx-ox or ty-oy):  # point must lie on the original segment
                d=abs((px-ox)*(ty-oy)-(py-oy)*(tx-ox))/math.hypot(tx-ox,ty-oy); worst['line_off']=max(worst['line_off'],d)
        else:
            assert ml==mo, f"arc direction changed {ml} vs {mo}"
            cx,cy=ox+oi,oy+oj; lcx,lcy=lx+li_,ly+lj
            worst['arc_ctr']=max(worst['arc_ctr'],math.hypot(lcx-cx,lcy-cy))
            worst['arc_rad']=max(worst['arc_rad'],abs(math.hypot(px-cx,py-cy)-math.hypot(tx-cx,ty-cy)))
        if lx is not None and px is not None: worst['seg']=max(worst['seg'],math.hypot(px-lx,py-ly)*(1 if ml!=2 and ml!=3 else 1))
        if pz is not None and oz is not None and tz is not None and (px,py)!=(tx,ty):
            pass
        if px is not None and abs(px-tx)<6e-4 and abs(py-ty)<6e-4 and (tz is None or abs(pz-(tz+comp(tx,ty)))<0.002):
            worst['endxy']=max(worst['endxy'],abs(px-tx),abs(py-ty))
            if tz is not None: worst['endz']=max(worst['endz'],abs(pz-(tz+comp(tx,ty))))
            else: assert pz is None, "Z added while Z unknown"
            break
assert li==len(L), f"leveled has extra moves: {len(L)-li}"
print(f"2. {nO} original moves matched in order;  worst: end XY {worst['endxy']:.4f}  end Z vs (Z+surface) {worst['endz']:.4f}  "
      f"split-line off-path {worst['line_off']:.4f}  sub-arc centre {worst['arc_ctr']:.4f}  sub-arc radius {worst['arc_rad']:.4f}  longest move {worst['seg']:.2f} mm;  modal F mismatches {f_bad}")
# 3. intermediate split points: Z equals linear nominal + surface
bad=0; mx=0
for l in L:
    pass
cnt=lambda lines,pat: sum(1 for s in lines if re.match(pat, re.sub(r"\(.*?\)","",s).strip()))
print("3. G2/G3 lines: orig", cnt(orig,r"G[23]\b"), "-> leveled", cnt(lev,r"G[23]\b"), "| T-changes", [s for s in lev if re.match(r"T-?\d+M6",s)])
zs=[l[7] for l in L if l[0]=='m' and l[7] is not None]; zo=[o[7] for o in O if o[0]=='m' and o[7] is not None]
xs_=[l[5] for l in L if l[0]=='m' and l[5] is not None]; ys_=[l[6] for l in L if l[0]=='m' and l[6] is not None]
print(f"4. Z range orig {min(zo):.3f}..{max(zo):.3f}  leveled {min(zs):.4f}..{max(zs):.4f};  XY extents leveled X {min(xs_):.3f}..{max(xs_):.3f} Y {min(ys_):.3f}..{max(ys_):.3f}")
for (x,y) in [(XS[0],YS[0]),(XS[-1],YS[-1]),(min(xs_),max(ys_)),(max(xs_),max(ys_)),(min(xs_),min(ys_)),(max(xs_),min(ys_)),(XS[len(XS)//2],YS[len(YS)//2])]:
    print(f"   surface at X{x} Y{y}: {comp(x,y):+.4f}")
