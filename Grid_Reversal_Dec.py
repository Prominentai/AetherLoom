import os
import sys
import cv2
from PIL import Image
import numpy as np
import time
import shutil
from moviepy.editor import VideoFileClip, AudioFileClip




# 网格数量变量
grid_cols = 64  # 网格列数
grid_rows = int(grid_cols) + 2  # 网格行数（如16+2=18）

def add_audio_to_video(video_path, audio_path, output_path):
    """为视频添加音频"""
    try:
        video = VideoFileClip(video_path)
        audio = AudioFileClip(audio_path)
        video = video.set_audio(audio)
        video.write_videofile(output_path, codec='libx264', audio_codec='aac')
        print(f"✓ 音频已合成到视频: {output_path}")
        return True
    except Exception as e:
        print(f"✗ 音视频合成失败: {e}")
        return False

def reverse_image_grid(input_path, output_path):
    """反转图像网格"""
    try:
        start_time = time.time()

        img = Image.open(input_path)
        width, height = img.size
        tile_width = width // grid_cols
        tile_height = height // grid_rows

        print(f"处理: {os.path.basename(input_path)}")
        print(f"    图像尺寸: {width}x{height}")
        print(f"    每个网格: {tile_width}x{tile_height}")
        print(f"    去除水印，修复后尺寸: {width}x{tile_height * grid_cols}")

        restored_img = Image.new('RGB', (width, tile_height * grid_cols))

        for row in range(grid_cols):
            for col in range(grid_cols):
                reversed_row = grid_rows - 1 - row
                reversed_col = grid_cols - 1 - col
                left = reversed_col * tile_width
                upper = reversed_row * tile_height
                right = left + tile_width
                lower = upper + tile_height
                tile = img.crop((left, upper, right, lower))
                restore_x = col * tile_width
                restore_y = row * tile_height
                restored_img.paste(tile, (restore_x, restore_y))

        restored_img.save(output_path)

        end_time = time.time()
        print(f"    解码耗时: {end_time - start_time:.2f} 秒")
        return True
    except Exception as e:
        print(f"✗ 处理失败 {os.path.basename(input_path)}: {e}")
        return False

def restore_video_cv2(input_path, output_path):
    """处理视频或GIF，不保留音频（GIF不支持音频）"""
    try:
        start_time = time.time()
        _, ext = os.path.splitext(input_path)
        ext = ext.lower()

        # GIF 文件处理
        if ext == '.gif':
            print(f"处理GIF动画: {os.path.basename(input_path)}")
            img = Image.open(input_path)
            width, height = img.size
            tile_width = width // grid_cols
            tile_height = height // grid_rows

            frames = []
            frame_count = 0
            try:
                while True:
                    frame = img.copy()
                    restored_img = Image.new('RGB', (width, tile_height * grid_cols))
                    for row in range(grid_cols):
                        for col in range(grid_cols):
                            reversed_row = grid_rows - 1 - row
                            reversed_col = grid_cols - 1 - col
                            left = reversed_col * tile_width
                            upper = reversed_row * tile_height
                            right = left + tile_width
                            lower = upper + tile_height
                            tile = frame.crop((left, upper, right, lower))
                            restore_x = col * tile_width
                            restore_y = row * tile_height
                            restored_img.paste(tile, (restore_x, restore_y))
                    frames.append(restored_img)
                    frame_count += 1
                    img.seek(img.tell() + 1)
            except EOFError:
                pass  # GIF读取结束

            # 打印GIF信息
            duration = img.info.get('duration', 100) * frame_count / 1000  # 秒
            print(f"    GIF尺寸: {width}x{height}")
            print(f"    每个网格: {tile_width}x{tile_height}")
            print(f"    去除水印，修复后尺寸: {width}x{tile_height * grid_cols}")
            print(f"    总帧数: {frame_count}")
            print(f"    时长: {duration:.2f} 秒")
            print(f"    是否有音频: 否")

            frames[0].save(
                output_path,
                save_all=True,
                append_images=frames[1:],
                duration=img.info.get('duration', 100),
                loop=0
            )
            print(f"✓ GIF修复完成: {output_path}")

        # 其他视频格式处理
        else:
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                print(f"✗ 无法打开视频: {input_path}")
                return False

            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            tile_width = width // grid_cols
            tile_height = height // grid_rows

            print(f"处理视频: {os.path.basename(input_path)}")
            print(f"    视频尺寸: {width}x{height}")
            print(f"    每个网格: {tile_width}x{tile_height}")
            print(f"    去除水印，修复后尺寸: {width}x{tile_height * grid_cols}")
            print(f"    帧率: {fps}")
            print(f"    总帧数: {frame_count}")

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            temp_video = output_path + ".tmp.mp4"
            out = cv2.VideoWriter(temp_video, fourcc, fps, (width, tile_height * grid_cols))
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(img)
                restored_img = Image.new('RGB', (width, tile_height * grid_cols))
                for row in range(grid_cols):
                    for col in range(grid_cols):
                        reversed_row = grid_rows - 1 - row
                        reversed_col = grid_cols - 1 - col
                        left = reversed_col * tile_width
                        upper = reversed_row * tile_height
                        right = left + tile_width
                        lower = upper + tile_height
                        tile = pil_img.crop((left, upper, right, lower))
                        restore_x = col * tile_width
                        restore_y = row * tile_height
                        restored_img.paste(tile, (restore_x, restore_y))
                out_frame = cv2.cvtColor(np.array(restored_img), cv2.COLOR_RGB2BGR)
                out.write(out_frame)
            cap.release()
            out.release()

            # 尝试保留音频
            try:
                orig_clip = VideoFileClip(input_path)
                video_clip = VideoFileClip(temp_video)
                if orig_clip.audio is not None:
                    video_clip = video_clip.set_audio(orig_clip.audio)
                video_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
                os.remove(temp_video)
                print(f"✓ 修复后视频已保留原音频: {output_path}")
            except Exception as e:
                shutil.move(temp_video, output_path)
                print(f"✗ 音频合成失败，仅保存无音频视频: {output_path}, 错误: {e}")

                print(f"处理视频: {os.path.basename(input_path)}")
                print(f"    视频尺寸: {width}x{height}")
                print(f"    每个网格: {tile_width}x{tile_height}")
                print(f"    去除水印，修复后尺寸: {width}x{tile_height * grid_cols}")
                print(f"    帧率: {fps}")
                print(f"    总帧数: {frame_count}")

                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                temp_video = output_path + ".tmp.mp4"
                out = cv2.VideoWriter(temp_video, fourcc, fps, (width, tile_height * grid_cols))
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        break
                    img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(img)
                    restored_img = Image.new('RGB', (width, tile_height * grid_cols))
                    for row in range(grid_cols):
                        for col in range(grid_cols):
                            reversed_row = grid_rows - 1 - row
                            reversed_col = grid_cols - 1 - col
                            left = reversed_col * tile_width
                            upper = reversed_row * tile_height
                            right = left + tile_width
                            lower = upper + tile_height
                            tile = pil_img.crop((left, upper, right, lower))
                            restore_x = col * tile_width
                            restore_y = row * tile_height
                            restored_img.paste(tile, (restore_x, restore_y))
                    out_frame = cv2.cvtColor(np.array(restored_img), cv2.COLOR_RGB2BGR)
                    out.write(out_frame)
                cap.release()
                out.release()

                # 尝试保留音频
                try:
                    orig_clip = VideoFileClip(input_path)
                    video_clip = VideoFileClip(temp_video)
                    if orig_clip.audio is not None:
                        video_clip = video_clip.set_audio(orig_clip.audio)
                    video_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
                    os.remove(temp_video)
                    print(f"✓ 修复后视频已保留原音频: {output_path}")
                except Exception as e:
                    shutil.move(temp_video, output_path)
                    print(f"✗ 音频合成失败，仅保存无音频视频: {output_path}, 错误: {e}")

        end_time = time.time()
        print(f"    解码耗时: {end_time - start_time:.2f} 秒")
        return True

    except Exception as e:
        print(f"✗ 视频修复失败 {os.path.basename(input_path)}: {e}")
        return False

def main():
    """主函数"""
    print("=" * 50)
    print("""
 ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄                                                                   
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌                                                                  
▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌ ▀▀▀▀█░█▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌                                                                 
▐░▌          ▐░▌       ▐░▌     ▐░▌     ▐░▌       ▐░▌                                                                 
▐░▌ ▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌     ▐░▌     ▐░▌       ▐░▌                                                                 
▐░▌▐░░░░░░░░▌▐░░░░░░░░░░░▌     ▐░▌     ▐░▌       ▐░▌                                                                 
▐░▌ ▀▀▀▀▀▀█░▌▐░█▀▀▀▀█░█▀▀      ▐░▌     ▐░▌       ▐░▌                                                                 
▐░▌       ▐░▌▐░▌     ▐░▌       ▐░▌     ▐░▌       ▐░▌                                                                 
▐░█▄▄▄▄▄▄▄█░▌▐░▌      ▐░▌  ▄▄▄▄█░█▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌                                                                 
▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌                                                                  
 ▀▀▀▀▀▀▀▀▀▀▀  ▀         ▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀                                                                   
                                                                                                                     
 ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄               ▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄                  
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌             ▐░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌                 
▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀  ▐░▌           ▐░▌ ▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░▌                 
▐░▌       ▐░▌▐░▌            ▐░▌         ▐░▌  ▐░▌          ▐░▌       ▐░▌▐░▌          ▐░▌       ▐░▌▐░▌                 
▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄    ▐░▌       ▐░▌   ▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌▐░▌                 
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌    ▐░▌     ▐░▌    ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌                 
▐░█▀▀▀▀█░█▀▀ ▐░█▀▀▀▀▀▀▀▀▀      ▐░▌   ▐░▌     ▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀█░█▀▀  ▀▀▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░▌                 
▐░▌     ▐░▌  ▐░▌                ▐░▌ ▐░▌      ▐░▌          ▐░▌     ▐░▌            ▐░▌▐░▌       ▐░▌▐░▌                 
▐░▌      ▐░▌ ▐░█▄▄▄▄▄▄▄▄▄        ▐░▐░▌       ▐░█▄▄▄▄▄▄▄▄▄ ▐░▌      ▐░▌  ▄▄▄▄▄▄▄▄▄█░▌▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄▄▄        
▐░▌       ▐░▌▐░░░░░░░░░░░▌        ▐░▌        ▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌       
 ▀         ▀  ▀▀▀▀▀▀▀▀▀▀▀          ▀          ▀▀▀▀▀▀▀▀▀▀▀  ▀         ▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀         ▀  ▀▀▀▀▀▀▀▀▀▀▀        
                                                                                                                     
 ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄                                                     
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌                                                    
▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀▀▀                                                     
▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌                                                              
▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌                                                              
▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░▌                                                              
▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░▌                                                              
▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌                                                              
▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄▄▄                                                     
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌                                                    
 ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀   ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀                                                     
                                                                                                                     
                           ▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄   ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄ 
                          ▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌
                          ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌
                          ▐░▌       ▐░▌▐░▌          ▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌       ▐░▌
 ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄ ▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌
▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌
 ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀ ▐░▌       ▐░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀█░█▀▀ 
                          ▐░▌       ▐░▌▐░▌          ▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌     ▐░▌  
                          ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌      ▐░▌ 
                          ▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░▌ ▐░░░░░░░░░░░▌▐░▌       ▐░▌
                           ▀▀▀▀▀▀▀▀▀▀   ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀   ▀▀▀▀▀▀▀▀▀▀▀  ▀         ▀ 
          """)
    print("=" * 50)
    
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(base_dir, "解码前")
    output_dir = os.path.join(base_dir, "解码后")

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print(f"\n扫描目录: {input_dir}")

    image_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')
    video_extensions = ('.mp4', '.mov', '.avi', '.webm', '.gif')
    processed_count = 0
    image_count = 0
    video_count = 0

    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
        print(f"输入文件夹不存在，已自动创建: {input_dir}")
        print("请将原图放入该文件夹后重新运行程序。")
        print("7秒后自动退出...")
        time.sleep(7)
        return

    input_files = os.listdir(input_dir)
    if not input_files:
        print(f"✗ 输入文件夹为空: {input_dir}")
        print("7秒后自动退出...")
        time.sleep(7)
        return

    for filename in input_files:
        filepath = os.path.join(input_dir, filename)
        name, ext = os.path.splitext(filename)
        output_filename = f"{name}_restored{ext}"
        output_path = os.path.join(output_dir, output_filename)

        if os.path.exists(output_path):
            continue

        if (filename.lower().endswith(image_extensions) and 
            'restored' not in filename.lower() and 
            os.path.isfile(filepath)):
            if reverse_image_grid(filepath, output_path):
                processed_count += 1
                image_count += 1
                print(f"✓ 已保存: {output_filename}\n")

        elif (filename.lower().endswith(video_extensions) and 
              'restored' not in filename.lower() and 
              os.path.isfile(filepath)):
            if restore_video_cv2(filepath, output_path):
                processed_count += 1
                video_count += 1
                print(f"✓ 已保存: {output_filename}\n")
            else:
                print(f"✗ 视频无法正常解码还原: {filename}")

    print("=" * 50)
    if processed_count > 0:
        print(f"处理完成！共恢复了 {processed_count} 个文件")
        print(f"    图像文件: {image_count} 个")
        print(f"    视频/GIF 文件: {video_count} 个")
    else:
        print("未找到需要处理的文件")
        print("提示: 请确保文件正确放在“解码前”文件夹内")
    
    print("7秒后自动退出...")
    time.sleep(7)

if __name__ == "__main__":
    main()
