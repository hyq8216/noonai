"""Deterministic product-photo motion and cross-dissolve; no generated product motion."""
from PIL import Image
from core import Problem

MOTIONS=('none','push','pull','alternate')
TRANSITIONS=('cut','fade')
FPS=30
FADE=.4

def options(b,r):
    motion=b.get('motion','none');transition=b.get('transition','cut')
    if motion not in MOTIONS or transition not in TRANSITIONS:raise Problem('图组视频镜头或转场选项无效')
    # Keep canonical recipes and replay hashes unchanged for legacy static videos.
    if motion!='none' or transition!='cut':r.update(motion=motion,transition=transition)

def duration(r,count):return count*r['seconds']-(count-1)*FADE if r.get('transition')=='fade' else count*r['seconds']

def arguments(media,r,assets,tmp,jid):
    from media import SIZES,rgb_image,fit
    w,h=SIZES[r['aspect']];frames=max(1,round(r['seconds']*FPS));args=[];filters=[]
    for i,a in enumerate(assets):
        media.check_cancel(jid)
        # Keep every source pixel inside the composition, including at maximum 4% push.
        canvas=Image.new('RGB',(w,h),r['background']);box=(int(w*.92),int(h*.92))
        canvas.paste(fit(rgb_image(media.root/a['file']),box,r['background']),((w-box[0])//2,(h-box[1])//2))
        path=tmp/f'motion-{i:03}.png';canvas.save(path)
        args+=['-loop','1','-framerate',str(FPS),'-t',str(r['seconds']),'-i',str(path)]
        motion=r.get('motion','none')
        if motion=='alternate':motion='push' if i%2==0 else 'pull'
        # Smooth cubic ease keeps the start and end velocity at zero.
        t=f'min(on/{max(1,frames-1)},1)';ease=f'({t}*{t}*(3-2*{t}))'
        z=f'1+0.04*{ease}' if motion=='push' else f'1.04-0.04*{ease}'
        vf=f"scale={w*2}:{h*2}:flags=lanczos,zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={FPS}," if motion!='none' else ''
        filters.append(f'[{i}:v]{vf}trim=duration={r["seconds"]},setpts=PTS-STARTPTS,setsar=1,settb=AVTB,format=yuv444p,fps={FPS}[v{i}]')
    if len(assets)==1:label='v0'
    elif r.get('transition')=='fade':
        label='v0'
        for i in range(1,len(assets)):
            out=f'x{i}';offset=i*(r['seconds']-FADE)
            filters.append(f'[{label}][v{i}]xfade=transition=fade:duration={FADE}:offset={offset},fps={FPS}[{out}]');label=out
    else:
        filters.append(''.join(f'[v{i}]' for i in range(len(assets)))+f'concat=n={len(assets)}:v=1:a=0[joined]');label='joined'
    filters.append(f'[{label}]format=yuv420p[video]')
    return args+['-filter_complex_threads','2','-filter_complex',';'.join(filters),'-map','[video]','-t',str(duration(r,len(assets))),'-r',str(FPS),'-an']
