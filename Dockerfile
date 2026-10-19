# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# SEW_web 运行环境（浏览器端无需 Python；所有依赖都在镜像里）
#   新增依赖：powerbin（PowerBin 分 bin）、plotbin；ppxf 固定 9.5.0
#   构建：docker build -t sew-web .
#   运行：docker run --rm -p 8765:8765 -v /host/fits:/data sew-web
# ---------------------------------------------------------------------------

# ---------- 构建阶段：装依赖（需要编译器时只留在本阶段） ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gfortran \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /build
COPY requirements.txt .
# 可选：国内镜像源，例 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=
RUN pip install --upgrade pip setuptools wheel \
 && if [ -n "$PIP_INDEX_URL" ]; then \
      pip install -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install -r requirements.txt; \
    fi

# ---------- 运行阶段 ----------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    PATH=/opt/venv/bin:$PATH \
    SEW_TEMPLATE_DIR=/app/data/templates

# libgomp1: numpy/scipy/ppxf 的 OpenMP 运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY . .
RUN mkdir -p /app/data/uploads /app/data/templates \
 && useradd -u 1000 -m -s /bin/bash sew \
 && chown -R sew:sew /app

USER sew

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8765"]
