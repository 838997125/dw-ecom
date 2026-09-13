#!/bin/bash
export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$HOME/.local/bin:$PATH"
cd ~/.openclaw/workspace/dw-ecom

TABLES=(
    "SALENOTESMT"
    "SALENOTESDT"
    "BATCHCODE"
    "K_D3OMS_STOCKOUTMT_PLAN"
    "PURORDERMT"
    "PURORDERDT"
    "PURINMT"
    "PURINDT"
    "ANGLEBALANCE"
    "K_D3OMS_ORDERREFUNDMT"
    "K_D3OMS_ORDERREFUNDDT"
)

for TABLE in "${TABLES[@]}"; do
    echo "$(date): === 开始同步 $TABLE ==="
    python3 -m src.cdc.cdc full --table "$TABLE" 2>&1 | grep -E "(INFO|ERROR|WARNING)" | tail -5
    echo "$(date): === $TABLE 完成 ==="
    echo ""
done

echo "$(date): 全部同步完成"
python3 -m src.cdc.cdc status 2>&1
python3 -m src.cdc.cdc verify --all 2>&1
