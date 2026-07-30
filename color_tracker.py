#!/usr/bin/env python3
"""
CCASS Color Tracking (染色追蹤) Engine
Tracks share flows between brokers over time to detect concentration patterns.
"""
import json, sys, os
from datetime import datetime

# Distinct colors for top brokers (tailwind-compatible)
BROKER_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#f43f5e", "#84cc16",
]

def analyze_color_tracking(data_points):
    """
    Analyze CCASS data over multiple dates to track position color flows.
    
    Args:
        data_points: List of {date, participants: [{id, name, shares, percentage}]}
    
    Returns:
        Color tracking analysis with flows, concentration trends
    """
    if len(data_points) < 2:
        return {"error": "Need at least 2 dates for tracking"}
    
    # Step 1: Track all broker IDs across all dates
    all_brokers = {}
    for dp in data_points:
        for p in dp.get('participants', []):
            pid = p.get('participant_id', p.get('id', ''))
            if pid not in all_brokers:
                all_brokers[pid] = {
                    'id': pid,
                    'name': p.get('name', 'N/A'),
                    'appearances': 0,
                    'total_net_flow': 0,
                    'first_shares': 0,
                    'last_shares': 0,
                    'first_pct': 0.0,
                    'last_pct': 0.0,
                }
    
    # Step 2: Calculate flows between consecutive dates
    flows = []
    for i in range(len(data_points) - 1):
        prev = {p.get('participant_id', p.get('id', '')): p for p in data_points[i].get('participants', [])}
        curr = {p.get('participant_id', p.get('id', '')): p for p in data_points[i+1].get('participants', [])}
        
        period = f"{data_points[i]['date']} → {data_points[i+1]['date']}"
        
        for pid in set(list(prev.keys()) + list(curr.keys())):
            p_prev = prev.get(pid, {'shares': 0, 'percentage': 0.0, 'name': 'N/A'})
            p_curr = curr.get(pid, {'shares': 0, 'percentage': 0.0, 'name': p_prev.get('name','N/A')})
            
            delta = p_curr['shares'] - p_prev['shares']
            pct_delta = round(p_curr['percentage'] - p_prev['percentage'], 3)
            
            if delta != 0:
                flows.append({
                    'broker_id': pid,
                    'broker_name': p_curr.get('name') or p_prev.get('name', 'N/A'),
                    'period': period,
                    'delta_shares': delta,
                    'delta_pct': pct_delta,
                    'shares_before': p_prev['shares'],
                    'shares_after': p_curr['shares'],
                })
            
            if pid in all_brokers:
                all_brokers[pid]['appearances'] += 1
                all_brokers[pid]['total_net_flow'] += delta
    
    # Step 3: Set first/last positions
    first_date_participants = {p.get('participant_id', p.get('id', '')): p for p in data_points[0].get('participants', [])}
    last_date_participants = {p.get('participant_id', p.get('id', '')): p for p in data_points[-1].get('participants', [])}
    
    for pid, broker in all_brokers.items():
        if pid in first_date_participants:
            broker['first_shares'] = first_date_participants[pid]['shares']
            broker['first_pct'] = first_date_participants[pid]['percentage']
        if pid in last_date_participants:
            broker['last_shares'] = last_date_participants[pid]['shares']
            broker['last_pct'] = last_date_participants[pid]['percentage']
    
    # Step 4: Classify brokers as accumulators vs distributors
    accumulators = []
    distributors = []
    
    for pid, broker in all_brokers.items():
        net_change = broker['last_pct'] - broker['first_pct']
        if net_change > 0.05:  # Threshold: 0.05% change
            accumulators.append({
                'id': pid,
                'name': broker['name'],
                'net_change': round(net_change, 3),
                'net_flow': broker['total_net_flow'],
                'first_pct': broker['first_pct'],
                'last_pct': broker['last_pct'],
            })
        elif net_change < -0.05:
            distributors.append({
                'id': pid,
                'name': broker['name'],
                'net_change': round(net_change, 3),
                'net_flow': broker['total_net_flow'],
                'first_pct': broker['first_pct'],
                'last_pct': broker['last_pct'],
            })
    
    accumulators.sort(key=lambda x: x['net_change'], reverse=True)
    distributors.sort(key=lambda x: x['net_change'])
    
    # Step 5: Assign colors to top brokers
    top_brokers = sorted(all_brokers.values(), key=lambda x: abs(x['total_net_flow']), reverse=True)[:10]
    color_map = {}
    for i, broker in enumerate(top_brokers):
        color_map[broker['id']] = {
            'id': broker['id'],
            'name': broker['name'],
            'color': BROKER_COLORS[i % len(BROKER_COLORS)],
            'net_flow': broker['total_net_flow'],
            'is_accumulator': broker['last_pct'] > broker['first_pct'],
        }
    
    # Step 6: Concentration trend analysis
    total_accumulated = sum(a['net_change'] for a in accumulators)
    top3_share = sum(a['net_change'] for a in accumulators[:3]) if accumulators else 0
    concentration_ratio = (top3_share / total_accumulated * 100) if total_accumulated > 0 else 0
    
    # Determine trend
    if concentration_ratio > 70 and len(accumulators) <= 5:
        trend = "🔴 高度集中 — 少數倉位主導收貨，歸邊信號強"
    elif concentration_ratio > 50 and len(accumulators) >= 3:
        trend = "🟡 中度集中 — 部分倉位持續吸納，值得監察"
    elif len(accumulators) == 0:
        trend = "⚪ 無明顯集中 — 倉位變化分散"
    else:
        trend = "🟢 分散狀態 — 未見歸邊信號"
    
    return {
        'stock_code': data_points[0].get('stock_code', 'N/A'),
        'date_range': f"{data_points[0]['date']} → {data_points[-1]['date']}",
        'total_dates': len(data_points),
        'total_flows': len(flows),
        'accumulators': accumulators[:10],
        'distributors': distributors[:10],
        'color_map': list(color_map.values()),
        'concentration_ratio': round(concentration_ratio, 1),
        'trend': trend,
        'top_flows': sorted(flows, key=lambda x: abs(x['delta_shares']), reverse=True)[:30],
        'broker_count': len(all_brokers),
    }


if __name__ == '__main__':
    # Read JSON array of CCASS data points from stdin
    data = json.load(sys.stdin)
    if isinstance(data, dict):
        data = [data]
    result = analyze_color_tracking(data)
    print(json.dumps(result, ensure_ascii=False, indent=2))
