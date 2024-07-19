CUDA_DEVICE_MAX_CONNECTIONS=1 python -m torch.distributed.run \
    --nproc_per_node 4 \
    --nnodes 1 \
    run_train.py --config-file experiments/llama_3_8b.yaml