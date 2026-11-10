这个目录必须放 docker save 出来的 .tar 镜像包。
在**有 Docker 的机器**上执行：
    python make_payload.py --save-images
然后把 payload/images/*.tar 一起交付。缺镜像 → 安装程序第 05 步直接失败。
