"""
健康检查API路由
"""

from fastapi import APIRouter
from datetime import datetime
from typing import Dict, Any

from backend import __version__

router = APIRouter()


@router.get("/")
async def health_check() -> Dict[str, Any]:
    """健康检查端点."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": __version__
    }


@router.get("/video-categories")
async def get_video_categories() -> Dict[str, Any]:
    """获取视频分类配置."""
    return {
        "categories": [
            {
                "value": "knowledge",
                "name": "知识科普",
                "description": "Knowledge content such as science, technology, history, and culture",
                "icon": "book",
                "color": "#1890ff"
            },
            {
                "value": "entertainment", 
                "name": "Entertainment",
                "description": "Entertainment content such as games, music, movies, and variety shows",
                "icon": "play-circle",
                "color": "#52c41a"
            },
            {
                "value": "experience",
                "name": "生活经验",
                "description": "Practical content such as life hacks, food, travel, and crafts",
                "icon": "heart",
                "color": "#fa8c16"
            },
            {
                "value": "opinion",
                "name": "观点评论",
                "description": "Current-affairs commentary, shared opinions, social topics, and more",
                "icon": "message",
                "color": "#722ed1"
            },
            {
                "value": "business",
                "name": "商业财经",
                "description": "Business analysis, financial news, investing, and more",
                "icon": "dollar",
                "color": "#13c2c2"
            },
            {
                "value": "speech",
                "name": "Talks & Interviews",
                "description": "Conversational content such as speeches, interviews, and dialogues",
                "icon": "sound",
                "color": "#eb2f96"
            }
        ],
        "default_category": "knowledge"
    } 