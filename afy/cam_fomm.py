import os, sys
import glob
import yaml
import time
import requests

import numpy as np
import cv2

from afy.videocaptureasync import VideoCaptureAsync
from afy.arguments import opt
from afy.utils import Once, log, crop, pad_img, resize, TicToc


from sys import platform as _platform
_streaming = False
if _platform == 'linux' or _platform == 'linux2':
    import pyfakewebcam
    _streaming = True
    
    
if _platform == 'darwin':
    if opt.worker_host is None:
        log('\nOnly remote GPU mode is supported for Mac (use --worker-host option to connect to the server)')
        log('Standalone version will be available lately!\n')
        exit()


def is_new_frame_better(source, driving, precitor):
    global avatar_kp
    global display_string
    
    if avatar_kp is None:
        display_string = "No face detected in avatar."
        return False
    
    if predictor.get_start_frame() is None:
        display_string = "No frame to compare to."
        return True
    
    driving_smaller = resize(driving, (128, 128))[..., :3]
    new_kp = predictor.get_frame_kp(driving)
    
    if new_kp is not None:
        new_norm = (np.abs(avatar_kp - new_kp) ** 2).sum()
        old_norm = (np.abs(avatar_kp - predictor.get_start_frame_kp()) ** 2).sum()
        
        out_string = "{0} : {1}".format(int(new_norm * 100), int(old_norm * 100))
        display_string = out_string
        log(out_string)
        
        return new_norm < old_norm
    else:
        display_string = "No face found!"
        return False


def load_stylegan_avatar():
    url = "https://thispersondoesnotexist.com/image"
    r = requests.get(url, headers={'User-Agent': "My User Agent 1.0"}).content

    image = np.frombuffer(r, np.uint8)
    image = cv2.imdecode(image, cv2.IMREAD_COLOR)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    image = resize(image, (IMG_SIZE, IMG_SIZE))

    return image


def change_avatar(predictor, new_avatar):
    global avatar, avatar_kp, kp_source
    avatar_kp = predictor.get_frame_kp(new_avatar)
    kp_source = None
    avatar = new_avatar
    predictor.set_source_image(avatar)


def draw_rect(img, rw=0.6, rh=0.8, color=(255, 0, 0), thickness=2):
    h, w = img.shape[:2]
    l = w * (1 - rw) // 2
    r = w - l
    u = h * (1 - rh) // 2
    d = h - u
    img = cv2.rectangle(img, (int(l), int(u)), (int(r), int(d)), color, thickness)


def print_help():
    log('\n\n=== Control keys ===')
    log('1-9: Change avatar')
    log('W: Zoom camera in')
    log('S: Zoom camera out')
    log('A: Previous avatar in folder')
    log('D: Next avatar in folder')
    log('Q: Get random avatar')
    log('X: Calibrate face pose')
    log('I: Show FPS')
    log('ESC: Quit')
    log('\nFull key list: https://github.com/alievk/avatarify#controls')
    log('\n\n')

if __name__ == "__main__":

    global display_string
    display_string = ""

    IMG_SIZE = 256

    if opt.no_stream:
        log('Force no streaming')
        _streaming = False

    log('Loading Predictor')
    predictor_args = {
        'config_path': opt.config,
        'checkpoint_path': opt.checkpoint,
        'relative': opt.relative,
        'adapt_movement_scale': opt.adapt_scale,
        'enc_downscale': opt.enc_downscale
    }
    if opt.is_worker:
        from afy import predictor_worker
        predictor_worker.run_worker(opt.worker_port)
        sys.exit(0)
    elif opt.worker_host:
        from afy import predictor_remote
        predictor = predictor_remote.PredictorRemote(
            worker_host=opt.worker_host, worker_port=opt.worker_port,
            **predictor_args
        )
    else:
        from afy import predictor_local
        predictor = predictor_local.PredictorLocal(
            **predictor_args
        )

    avatars=[]
    images_list = sorted(glob.glob(f'{opt.avatars}/*'))
    for i, f in enumerate(images_list):
        if f.endswith('.jpg') or f.endswith('.jpeg') or f.endswith('.png'):
            key = len(avatars) + 1
            log(f'Key {key}: {f}')
            img = cv2.imread(f)
            if img.ndim == 2:
                img = np.tile(img[..., None], [1, 1, 3])
            img = img[..., :3][..., ::-1]
            img = resize(img, (IMG_SIZE, IMG_SIZE))
            avatars.append(img)


    cap = VideoCaptureAsync(opt.cam)
    cap.start()

    if _streaming:
        ret, frame = cap.read()
        stream_img_size = frame.shape[1], frame.shape[0]
        stream = pyfakewebcam.FakeWebcam(f'/dev/video{opt.virt_cam}', *stream_img_size)

    cur_ava = 0    
    avatar = None
    change_avatar(predictor, avatars[cur_ava])
    passthrough = False

    cv2.namedWindow('cam', cv2.WINDOW_GUI_NORMAL)
    cv2.moveWindow('cam', 500, 250)

    frame_proportion = 0.9
    frame_offset_x = 0
    frame_offset_y = 0

    overlay_alpha = 0.0
    preview_flip = False
    output_flip = False
    find_keyframe = False
    is_calibrated = False

    fps_hist = []
    fps = 0
    show_fps = False

    print_help()

    try:
        while True:
            tt = TicToc()

            timing = {
                'preproc': 0,
                'predict': 0,
                'postproc': 0
            }

            green_overlay = False

            tt.tic()

            ret, frame = cap.read()
            if not ret:
                log("Can't receive frame (stream end?). Exiting ...")
                break

            frame = frame[..., ::-1]
            frame_orig = frame.copy()

            frame, lrudwh = crop(frame, p=frame_proportion, offset_x=frame_offset_x, offset_y=frame_offset_y)
            frame_lrudwh = lrudwh
            frame = resize(frame, (IMG_SIZE, IMG_SIZE))[..., :3]

            if find_keyframe:
                if is_new_frame_better(avatar, frame, predictor):
                    log("Taking new frame!")
                    green_overlay = True
                    predictor.reset_frames()

            timing['preproc'] = tt.toc()

            if passthrough:
                out = frame
            else:
                tt.tic()
                pred = predictor.predict(frame)
                out = pred
                timing['predict'] = tt.toc()

            tt.tic()

            if not opt.no_pad:
                out = pad_img(out, stream_img_size)
            
            key = cv2.waitKey(1)

            if key == 27: # ESC
                break
            elif key == ord('d'):
                cur_ava += 1
                if cur_ava >= len(avatars):
                    cur_ava = 0
                passthrough = False
                change_avatar(predictor, avatars[cur_ava])
            elif key == ord('a'):
                cur_ava -= 1
                if cur_ava < 0:
                    cur_ava = len(avatars) - 1
                passthrough = False
                change_avatar(predictor, avatars[cur_ava])
            elif key == ord('w'):
                frame_proportion -= 0.05
                frame_proportion = max(frame_proportion, 0.1)
            elif key == ord('s'):
                frame_proportion += 0.05
                frame_proportion = min(frame_proportion, 1.0)
            elif key == ord('H'):
                if frame_lrudwh[0] - 1 > 0:
                    frame_offset_x -= 1
            elif key == ord('h'):
                if frame_lrudwh[0] - 5 > 0:
                    frame_offset_x -= 5
            elif key == ord('K'):
                if frame_lrudwh[1] + 1 < frame_lrudwh[4]:
                    frame_offset_x += 1
            elif key == ord('k'):
                if frame_lrudwh[1] + 5 < frame_lrudwh[4]:
                    frame_offset_x += 5
            elif key == ord('J'):
                if frame_lrudwh[2] - 1 > 0:
                    frame_offset_y -= 1
            elif key == ord('j'):
                if frame_lrudwh[2] - 5 > 0:
                    frame_offset_y -= 5
            elif key == ord('U'):
                if frame_lrudwh[3] + 1 < frame_lrudwh[5]:
                    frame_offset_y += 1
            elif key == ord('u'):
                if frame_lrudwh[3] + 5 < frame_lrudwh[5]:
                    frame_offset_y += 5
            elif key == ord('Z'):
                frame_offset_x = 0
                frame_offset_y = 0
                frame_proportion = 0.9
            elif key == ord('x'):
                predictor.reset_frames()

                if not is_calibrated:
                    cv2.namedWindow('avatarify', cv2.WINDOW_GUI_NORMAL)
                    cv2.moveWindow('avatarify', 600, 250)
                
                is_calibrated = True
            elif key == ord('z'):
                overlay_alpha = max(overlay_alpha - 0.1, 0.0)
            elif key == ord('c'):
                overlay_alpha = min(overlay_alpha + 0.1, 1.0)
            elif key == ord('r'):
                preview_flip = not preview_flip
            elif key == ord('t'):
                output_flip = not output_flip
            elif key == ord('f'):
                find_keyframe = not find_keyframe
            elif key == ord('q'):
                try:
                    log('Loading StyleGAN avatar...')
                    avatar = load_stylegan_avatar()
                    passthrough = False
                    change_avatar(predictor, avatar)
                except:
                    log('Failed to load StyleGAN avatar')
            elif key == ord('i'):
                show_fps = not show_fps
            elif 48 < key < 58:
                cur_ava = min(key - 49, len(avatars) - 1)
                passthrough = False
                change_avatar(predictor, avatars[cur_ava])
            elif key == 48:
                passthrough = not passthrough
            elif key != -1:
                log(key)

            if _streaming:
                out = resize(out, stream_img_size)
                stream.schedule_frame(out)

            if overlay_alpha > 0:
                preview_frame = cv2.addWeighted( avatars[cur_ava], overlay_alpha, frame, 1.0 - overlay_alpha, 0.0)
            else:
                preview_frame = frame.copy()
            
            if preview_flip:
                preview_frame = cv2.flip(preview_frame, 1)
                
            if output_flip:
                out = cv2.flip(out, 1)
                
            if green_overlay:
                green_alpha = 0.8
                overlay = preview_frame.copy()
                overlay[:] = (0, 255, 0)
                preview_frame = cv2.addWeighted( preview_frame, green_alpha, overlay, 1.0 - green_alpha, 0.0)

            timing['postproc'] = tt.toc()
                
            if find_keyframe:
                preview_frame = cv2.putText(preview_frame, display_string, (10, 220), 0, 0.5 * IMG_SIZE / 256, (255, 255, 255), 1)

            if show_fps:
                timing_string = f"FPS/Model/Pre/Post: {fps:.1f} / {timing['predict']:.1f} / {timing['preproc']:.1f} / {timing['postproc']:.1f}"
                preview_frame = cv2.putText(preview_frame, timing_string, (10, 240), 0, 0.3 * IMG_SIZE / 256, (255, 255, 255), 1)

            if not is_calibrated:
                color = (0, 0, 255)
                thk = 2
                fontsz = 0.5
                preview_frame = cv2.putText(preview_frame, "FIT FACE IN RECTANGLE", (40, 20), 0, fontsz * IMG_SIZE / 255, color, thk)
                preview_frame = cv2.putText(preview_frame, "W - ZOOM IN", (60, 40), 0, fontsz * IMG_SIZE / 255, color, thk)
                preview_frame = cv2.putText(preview_frame, "S - ZOOM OUT", (60, 60), 0, fontsz * IMG_SIZE / 255, color, thk)
                preview_frame = cv2.putText(preview_frame, "THEN PRESS X", (60, 245), 0, fontsz * IMG_SIZE / 255, color, thk)

            if not opt.hide_rect:
                draw_rect(preview_frame)

            cv2.imshow('cam', preview_frame[..., ::-1])
            if is_calibrated:
                cv2.imshow('avatarify', out[..., ::-1])

            fps_hist.append(tt.toc(total=True))
            if len(fps_hist) == 10:
                fps = 10 / (sum(fps_hist) / 1000)
                fps_hist = []
    except KeyboardInterrupt:
        pass

    cap.stop()
    cv2.destroyAllWindows()
