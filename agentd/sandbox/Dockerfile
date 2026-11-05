FROM python:3.12-slim
RUN sed -i "s|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
# git/coreutils 是干活基础；libreoffice 只装 impress，用来把 ppt/docx 转 PDF/PNG 自检
RUN apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends \
       git coreutils grep sed gawk file zip unzip \
       libreoffice-impress libreoffice-writer --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# 文档处理常用库：装进镜像，省得每个任务现装（沙箱环境不持久）。
# 版本不锁死，pypi 白名单可达，需要新的可以现场 pip。
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    python-docx python-pptx openpyxl pandas xlsxwriter pillow

WORKDIR /work
