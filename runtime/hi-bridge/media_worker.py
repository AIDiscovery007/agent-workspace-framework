"""Offline, sandboxed audio/video decoder. Invoked by media_input.py only."""
import json
import sys
from pathlib import Path
import av
import numpy as np
from faster_whisper import WhisperModel

MAX_SECONDS=600
MODEL=Path(__file__).parent/'media-runtime/model'

def audio(path):
    samples=[];count=0
    with av.open(str(path),options={'protocol_whitelist':'file,pipe'}) as container:
        if not container.streams.audio:return None
        stream=container.streams.audio[0]
        if stream.duration and float(stream.duration*stream.time_base)>MAX_SECONDS:raise ValueError('音轨超过 10 分钟，请拆分。')
        resampler=av.AudioResampler(format='flt',layout='mono',rate=16000)
        for frame in container.decode(stream):
            for part in resampler.resample(frame):
                data=part.to_ndarray().reshape(-1);count+=data.size
                if count>MAX_SECONDS*16000:raise ValueError('音轨超过 10 分钟，请拆分。')
                samples.append(data)
        for part in resampler.resample(None):
            data=part.to_ndarray().reshape(-1);count+=data.size
            if count>MAX_SECONDS*16000:raise ValueError('音轨超过 10 分钟，请拆分。')
            samples.append(data)
    if not samples:return {'duration':0,'segments':[],'text':'未检测到音频样本'}
    model=WhisperModel(str(MODEL),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
    segments,info=model.transcribe(np.concatenate(samples),beam_size=5,vad_filter=True,condition_on_previous_text=False)
    entries=[{'start':round(s.start,2),'end':round(s.end,2),'text':s.text.strip()} for s in segments]
    return {'duration':round(count/16000,2),'language':info.language,'segments':entries,'text':'\n'.join(s['text'] for s in entries),'method':'local faster-whisper small; automatic transcript'}

def video(path,out):
    frames=[]
    with av.open(str(path),options={'protocol_whitelist':'file,pipe'}) as c:
        if not c.streams.video:raise ValueError('未发现视频轨道。')
        stream=c.streams.video[0]
        duration=float(stream.duration*stream.time_base) if stream.duration else float(c.duration or 0)/av.time_base
        if duration<=0 or duration>MAX_SECONDS:raise ValueError('视频时长未知或超过 10 分钟，请转为标准视频或拆分。')
        count=min(12,max(1,int(duration)+1))
        for i in range(count):
            target=i*max(0,duration-0.1)/max(1,count-1)
            c.seek(int(target/stream.time_base),stream=stream,backward=True)
            for j,frame in enumerate(c.decode(stream)):
                if j>500:raise ValueError('视频关键帧间隔过大，无法有界采样。')
                timestamp=float(frame.time or 0)
                if timestamp+0.1<target:continue
                if frame.width*frame.height>40_000_000:raise ValueError('视频分辨率过大。')
                image=frame.to_image();image.thumbnail((1280,1280))
                name=f'frame-{i:02d}.jpg';image.convert('RGB').save(out/name,quality=85)
                frames.append({'path':name,'seconds':round(timestamp,2)});break
    if not frames:raise ValueError('视频没有可解码画面。')
    return {'duration':duration,'frames':frames,'coverage':'均匀采样，最多12帧；不是逐帧审阅','audio':audio(path)}

if __name__=='__main__':
    kind,source,directory=sys.argv[1:];out=Path(directory)
    try:
        result=video(Path(source),out) if kind=='video' else {'audio':audio(Path(source))}
        if kind=='voice' and result['audio'] is None:raise ValueError('语音文件中没有音轨。')
        (out/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    except Exception as exc:
        # Decoder/transport internals may include private paths; do not expose them.
        print(str(exc) if isinstance(exc,ValueError) else '媒体解码或离线转写失败',file=sys.stderr)
        sys.exit(1)
