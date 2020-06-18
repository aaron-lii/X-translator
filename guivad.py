import ctypes
import inspect
import tkinter as tk
import webbrowser
from tkinter import ttk  # 导入ttk模块，因为下拉菜单控件在ttk中
from tkinter import scrolledtext
import queue
import time
import os
import _thread

import webrtcvad

import apiutil
import json
import sys
import sounddevice as sd
import soundfile as sf
import numpy
import threading
import contextlib
import wave
import collections


# ---------- 以下vad部分 ----------

def read_wave(path):
    with contextlib.closing(wave.open(path, 'rb')) as wf:
        num_channels = wf.getnchannels()
        assert num_channels == 1
        sample_width = wf.getsampwidth()
        assert sample_width == 2
        sample_rate = wf.getframerate()
        assert sample_rate in (8000, 16000, 32000, 48000)
        pcm_data = wf.readframes(wf.getnframes())
        return pcm_data


def write_wave(path, audio, sample_rate):
    with contextlib.closing(wave.open(path, 'wb')) as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio)


class Frame(object):
    """Represents a "frame" of audio data."""
    def __init__(self, bytes, timestamp, duration):
        self.bytes = bytes
        self.timestamp = timestamp
        self.duration = duration


def wav2vad(wavpath):
    global q_frames
    audio = read_wave(wavpath)

    n = int(16000 * (30 / 1000.0) * 2)
    offset = 0
    timestamp = 0.0
    duration = (float(n) / 16000) / 2.0
    while offset + n < len(audio):
        q_frames.put(Frame(audio[offset:offset + n], timestamp, duration))
        timestamp += duration
        offset += n
    os.remove(wavpath)


def vad_collector(sample_rate, frame_duration_ms, padding_duration_ms, vad, appid, appkey):

    global q_frames

    num_padding_frames = int(padding_duration_ms / frame_duration_ms)
    ring_buffer = collections.deque(maxlen=num_padding_frames)
    triggered = False

    voiced_frames = []
    chunk_i = 0
    while True:
        frame = q_frames.get()
        is_speech = vad.is_speech(frame.bytes, sample_rate)
        # 语音开始部分
        if not triggered:
            ring_buffer.append((frame, is_speech))
            num_voiced = len([f for f, speech in ring_buffer if speech])
            if num_voiced > 0.9 * ring_buffer.maxlen:
                triggered = True
                for f, s in ring_buffer:
                    voiced_frames.append(f)
                ring_buffer.clear()
        # 语音结束部分
        else:
            voiced_frames.append(frame)
            ring_buffer.append((frame, is_speech))
            num_unvoiced = len([f for f, speech in ring_buffer if not speech])
            if num_unvoiced > 0.9 * ring_buffer.maxlen or len(voiced_frames) > 133:
                # or len(voiced_frames) > 165
                triggered = False
                # 切完音频并发送给识别
                segment = b''.join([f.bytes for f in voiced_frames])
                path = 'tmp\\chunk-%d.wav' % (chunk_i)
                write_wave(path, segment, sample_rate)
                t_trans = threading.Thread(target=speech_trans, args=(appid, appkey, path, chunk_i))
                t_trans.setDaemon(True)
                t_trans.start()
                chunk_i = chunk_i + 1
                ring_buffer.clear()
                voiced_frames = []


def vad_main(vadlevel, appid, appkey):
    vad = webrtcvad.Vad(int(vadlevel))
    vad_collector(16000, 30, 300, vad, appid, appkey)
    # for i, segment in enumerate(segments):
    #     path = 'chunk-%002d.wav' % (i,)
    #     print(' Writing %s' % (path,))
    #     write_wave(path, segment, sample_rate)


# ---------- 以下录音及发送部分 ----------

def speech_trans(appid, appkey ,file_path, ff):

    global order
    global out1
    global out2
    global old_textcn
    global old_textjp

    app_key = appkey
    app_id = appid
    seq = 0
    for_mat = 6

    f = open(file_path, 'rb')
    chunk = f.read()
    end = 1

    ai_obj = apiutil.AiPlat(app_id, app_key)

    # start = time.perf_counter()
    rsp = ai_obj.getAaiWxAsrs(chunk, end, for_mat, seq)

    while True:
        if rsp['ret'] == 0 and ff == order:
            cn = json.dumps(rsp['data']['target_text'], ensure_ascii=False, sort_keys=False, indent=4)
            jp = json.dumps(rsp['data']['source_text'], ensure_ascii=False, sort_keys=False, indent=4)
            if cn[1:-1] != "":
                print(jp)
                print(cn)
                out1["text"] = old_textjp + "\n" + old_textcn
                out2["text"] = jp[1:-1] + "\n" + cn[1:-1]
                # print('----------------------API SUCC----------------------')
                old_textcn = cn[1:-1]
                old_textjp = jp[1:-1]
                order = order + 1
                break
            else:
                order = order + 1
                break
        elif rsp['ret'] != 0 and ff == order:
            print(json.dumps(rsp, ensure_ascii=False, sort_keys=False, indent=4))
            print('----------------------API FAIL----------------------')
            order = order + 1
            break

    # elapsed = (time.perf_counter() - start)
    # print("Time used:", elapsed)
    f.close()
    os.remove(file_path)


def callback(indata, frames, time, status):
    """This is called (from a separate thread) for each audio block."""
    if status:
        print(status, file=sys.stderr)
    q.put(indata.copy())


def start(appid, appkey, device):
    global is_start
    global order
    global t
    global t_vad
    global t_gui2

    if is_start:
        pass
    else:
        is_start = True
        write_json(appid, appkey)
        # 0.6秒录音一次并发送给audio队列
        t = threading.Thread(target=get_wav, args=(device,))
        t.setDaemon(True)
        t.start()
        # vad切完一段接发送给识别
        vadlevel = "2"
        t_vad = threading.Thread(target=vad_main, args=(vadlevel, appid, appkey,))
        t_vad.setDaemon(True)
        t_vad.start()

        # 显示结果的gui
        t_gui2 = threading.Thread(target=gui2, args=())
        t_gui2.setDaemon(True)
        t_gui2.start()


# 杀死线程
def _async_raise(tid, exctype):
    """raises the exception, performs cleanup if needed"""
    tid = ctypes.c_long(tid)
    if not inspect.isclass(exctype):
        exctype = type(exctype)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(exctype))
    if res == 0:
        raise ValueError("invalid thread id")
    elif res != 1:
        # """if it returns a number greater than one, you're in trouble,
        # and you should call it again with exc=NULL to revert the effect"""
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, None)
        raise SystemError("PyThreadState_SetAsyncExc failed")


def stop_thread():
    global window2
    global is_start
    global order

    is_start = False
    order = 0
    try:
        thread = t
        _async_raise(thread.ident, SystemExit)
        print("已结束识别！")
    except:
        print("没有识别线程！")

    try:
        thread2 = t_vad
        _async_raise(thread2.ident, SystemExit)
        print("已结束vad！")
    except:
        print("没有vad线程！")

    try:
        thread3 = t_gui2
        _async_raise(thread3.ident, SystemExit)
        print("已结束字幕！")
    except:
        print("没有字幕线程！")


def get_wav(device):

    print(device)
    ff = 1
    filename1 = "tmp\\rec_" + str(ff) + ".wav"

    try:
        try:
            os.remove(filename1)
        except:
            pass

        file1 = sf.SoundFile(filename1, mode='x', samplerate=16000, channels=1, subtype='PCM_16')

        with sd.InputStream(samplerate=16000, device=device, channels=1, callback=callback):
            print('#' * 80)
            print('press Ctrl+C to stop the recording')
            print('#' * 80)
            # 每秒约38块，每块416点
            i = 0
            while True:
                buffer1 = q.get()
                file1.write(buffer1)
                i = i + 1
                if i >= 22:
                    file1.flush()
                    file1.close()
                    # 发送给vad队列
                    wav2vad(filename1)
                    i = 0
                    ff = ff + 1
                    filename1 = "tmp\\rec_" + str(ff) + ".wav"
                    try:
                        os.remove(filename1)
                    except:
                        pass
                    file1 = sf.SoundFile(filename1, mode='x', samplerate=16000, channels=1, subtype='PCM_16')
    except KeyboardInterrupt:
        print('\nRecording finished: ' + "delme_rec_unlimited_.wav")


# ---------- 以下gui部分 ----------

def hit_me():
    global on_hit
    global ishide
    if on_hit == False:
        on_hit = True
        ishide = '*'
        # print(app_key.get())
    else:
        on_hit = False
        ishide = None
        # print('')


def open_url():
    webbrowser.open('https://ai.qq.com/console/', new=1, autoraise=True)


def listdevice(all):
    listdevice = []
    i = 0
    for device in all:
        listdevice.append(str(i) + " " + device["name"])
        i = i + 1
    return listdevice


def write_json(appid, appkey):
    data = [{'APPID': appid, 'APPKEY': appkey}]
    data_json = json.dumps(data)
    appmessage = open("appmessage.json", "w")
    appmessage.write(data_json)
    appmessage.close()


def read_json():
    try:
        appmessage = open("appmessage.json", "r")
        data_json = json.load(appmessage)
        return [data_json[0]["APPID"], data_json[0]["APPKEY"]]
    except:
        return ["", ""]


def test(x):
    print(x)


def gui():

    window = tk.Tk()
    screen_width = window.winfo_screenwidth()/2  # 获得屏幕宽度
    screen_height = window.winfo_screenheight()/2  # 获得屏幕高度
    window.title('Xi translator v0.1')
    window.geometry('450x300+%d+%d'%(screen_width-225, screen_height-150))

    app_key = tk.StringVar()
    app_key.set(read_json()[1])
    tk.Label(window, text='APPKEY : ').place(x=30, y=50)
    entry_app_key = tk.Entry(window, textvariable=app_key, show=None)
    entry_app_key.place(x=100, y=50)

    app_id = tk.StringVar()
    app_id.set(read_json()[0])
    tk.Label(window, text='APPID   : ').place(x=30, y=20)
    entry_app_id = tk.Entry(window, textvariable=app_id, show=None)
    entry_app_id.place(x=100, y=20)

    # 创建下拉菜单
    cmb = ttk.Combobox(window, width=30)
    tk.Label(window, text='录音驱动 : ').place(x=30, y=80)
    cmb.place(x=100, y=80)
    # 设置下拉菜单中的值
    cmb['value'] = (listdevice(sd.query_devices()))
    # 设置默认值，即默认下拉框中的内容
    cmb.current(0)
    # 执行函数
    def func(event):
        global device
        # print(cmb.get())
        print(cmb.current())
        device = cmb.current()
    cmb.bind("<<ComboboxSelected>>", func)

    # 点击按钮
    b1 = tk.Button(window, text='打开腾讯AI', font=('黑体', 12), width=12, height=1, command=open_url)
    b1.place(x=300, y=25)

    b2 = tk.Button(window, text='开始识别！', font=('黑体', 12), width=12, height=1, \
                   command=lambda: start(app_id.get(), app_key.get(), device))
    b2.place(x=300, y=125)

    b2 = tk.Button(window, text='结束识别', font=('黑体', 12), width=12, height=1, \
                   command=lambda: stop_thread())
    b2.place(x=170, y=125)

    b3 = tk.Button(window, text='点着玩', font=('黑体', 12), width=12, height=1)
    b3.place(x=30, y=125)

    # log输出框
    scr = scrolledtext.ScrolledText(window, width=50, height=7, wrap=tk.WORD)
    scr.place(x=30, y=180)

    def logout():
        while True:
            scr.insert('end', qr.get())
            scr.yview_moveto(1)

    logt = threading.Thread(target=logout, args=())
    logt.setDaemon(True)
    logt.start()

    window.mainloop()


def gui2():

    global out1
    global out2
    global window2

    window2 = tk.Tk()
    screen_width = window2.winfo_screenwidth() / 2  # 获得屏幕宽度
    screen_height = window2.winfo_screenheight() / 5*4  # 获得屏幕高度
    window2.title('sub')
    window2.geometry('600x80+%d+%d'%(screen_width-360, screen_width))
    window2['background'] = 'black'

    out1 = tk.Label(window2, text="1", font=('黑体', 12), bg='black', fg='white')
    out1.pack(side='top')

    out2 = tk.Label(window2, text="2", font=('黑体', 14), bg='black', fg='white')
    out2.pack(side='top')

    window2.overrideredirect(1)  # 去除窗口边框
    window2.wm_attributes("-alpha", 0.8)  # 透明度(0.0~1.0)
    window2.wm_attributes("-toolwindow", True)  # 置为工具窗口(没有最大最小按钮)
    window2.wm_attributes("-topmost", True)  # 永远处于顶层

    def StartMove(event):
        global x, y
        x = event.x
        y = event.y

    def StopMove(event):
        global x, y
        x = None
        y = None

    def OnMotion(event):
        global x, y
        deltax = event.x - x
        deltay = event.y - y
        window2.geometry("+%s+%s" % (window2.winfo_x() + deltax, window2.winfo_y() + deltay))
        window2.update()
        # print(event.x, event.y, window2.winfo_x(), window2.winfo_y(), window2.winfo_width(), window2.winfo_height())

    window2.bind("<ButtonPress-1>", StartMove)  # 监听左键按下操作响应函数
    window2.bind("<ButtonRelease-1>", StopMove)  # 监听左键松开操作响应函数
    window2.bind("<B1-Motion>", OnMotion)  # 监听鼠标移动操作响应函数

    def myquit():
        while True:
            if is_start == False:
                # window2.quit()
                window2.destroy()

    # e = tk.Button(window2, text='X', font=('黑体', 12), command=myquit)
    # e.place(x=10, y=10)

    t_quit = threading.Thread(target=myquit)
    t_quit.setDaemon(True)
    t_quit.start()

    window2.mainloop()


# 输出重定向
class redirect:
    content = ""
    def write(self,str):
        qr.put(str)
        # self.content += str
    def flush(self):
        # self.content = ""
        pass

if __name__ == '__main__':

    # 创建缓存文件夹
    tmp_path = ".\\tmp\\"
    isExists = os.path.exists(tmp_path)
    if not isExists:
        os.makedirs(tmp_path)
    else:
        pass

    on_hit = False
    order = 0
    is_start = False
    old_textcn = "cn"
    old_textjp = "jp"
    device = 0
    # print重定向
    r = redirect()
    sys.stdout = r

    q = queue.Queue()
    qr = queue.Queue()
    q_frames = queue.Queue()
    gui()
