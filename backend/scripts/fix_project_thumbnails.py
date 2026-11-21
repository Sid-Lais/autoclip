#!/usr/bin/env python3
"""
修复项目缩略图脚本
处理链接导入和文件导入项目的缩略图问题
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.core.database import SessionLocal
from backend.models.project import Project
from backend.utils.thumbnail_generator import generate_project_thumbnail
from backend.core.path_utils import get_projects_directory
import requests
import base64
import logging

logger = logging.getLogger(__name__)

def fix_project_thumbnail(project_id: str):
    """修复指定项目的缩略图"""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        
        if not project:
            print(f"❌ Project {project_id} not found")
            return False
        
        if project.thumbnail:
            print(f"✅ Project {project_id} already has a thumbnail, skipping")
            return True
        
        print(f"🔧 Fixing thumbnail for project {project_id}...")
        
        # 检查项目类型和来源
        source_url = project.project_metadata.get('source_url') if project.project_metadata else None
        is_bilibili_project = source_url and 'bilibili.com' in source_url
        has_video_file = project.video_path and Path(project.video_path).exists()
        
        if is_bilibili_project:
            # 链接导入项目 - 尝试从B站获取缩略图
            print(f"📺 Bilibili project detected, trying to fetch the original video thumbnail...")
            success = fix_bilibili_thumbnail(project, db)
        elif has_video_file:
            # 文件导入项目 - 从视频文件生成缩略图
            print(f"📁 File-import project detected, generating thumbnail from the video file...")
            success = fix_file_import_thumbnail(project, db)
        else:
            # 没有原始视频文件，尝试从切片生成缩略图
            print(f"🎬 No original video file, trying to generate a thumbnail from a clip...")
            success = fix_clip_thumbnail(project, db)
        
        if success:
            print(f"✅ Project {project_id} thumbnail fixed")
        else:
            print(f"❌ Project {project_id} thumbnail fix failed")
        
        return success
        
    except Exception as e:
        print(f"❌ Error while fixing thumbnail for project {project_id}: {e}")
        return False
    finally:
        db.close()

def fix_bilibili_thumbnail(project, db):
    """修复B站项目的缩略图"""
    try:
        # 从项目设置中获取B站信息
        if not project.processing_config:
            return False
        
        bilibili_info = project.processing_config.get('bilibili_info', {})
        if not bilibili_info:
            return False
        
        # 尝试从B站API获取缩略图
        # 这里需要根据实际的B站API来实现
        # 暂时返回False，表示需要手动处理
        print("⚠️  Fetching Bilibili thumbnails requires API support; skipping for now")
        return False
        
    except Exception as e:
        logger.error(f"修复B站缩略图失败: {e}")
        return False

def fix_file_import_thumbnail(project, db):
    """修复文件导入项目的缩略图"""
    try:
        video_path = Path(project.video_path)
        if not video_path.exists():
            print(f"⚠️  Video file does not exist: {video_path}")
            return False
        
        # 生成缩略图
        thumbnail_data = generate_project_thumbnail(project.id, video_path)
        
        if thumbnail_data:
            # 保存到数据库
            project.thumbnail = thumbnail_data
            db.commit()
            return True
        else:
            print("⚠️  Thumbnail generation failed")
            return False
            
    except Exception as e:
        logger.error(f"修复文件导入缩略图失败: {e}")
        return False

def fix_clip_thumbnail(project, db):
    """从切片生成缩略图"""
    try:
        # 查找项目目录中的切片文件
        project_dir = get_projects_directory() / str(project.id)
        clips_dir = project_dir / "output" / "clips"
        
        if not clips_dir.exists():
            print(f"⚠️  Clips directory does not exist: {clips_dir}")
            return False
        
        # 获取第一个切片文件
        clip_files = list(clips_dir.glob("*.mp4"))
        if not clip_files:
            print(f"⚠️  No clip files found")
            return False
        
        first_clip = clip_files[0]
        print(f"🎬 Generating thumbnail from clip file: {first_clip.name}")
        
        # 生成缩略图
        thumbnail_data = generate_project_thumbnail(project.id, first_clip)
        
        if thumbnail_data:
            # 保存到数据库
            project.thumbnail = thumbnail_data
            db.commit()
            return True
        else:
            print("⚠️  Failed to generate thumbnail from clip")
            return False
            
    except Exception as e:
        logger.error(f"从切片生成缩略图失败: {e}")
        return False

def fix_all_project_thumbnails():
    """修复所有项目的缩略图"""
    db = SessionLocal()
    try:
        # 查找所有没有缩略图的项目
        projects = db.query(Project).filter(Project.thumbnail.is_(None)).all()
        
        if not projects:
            print("✅ All projects already have thumbnails")
            return True
        
        print(f"📋 Found {len(projects)} projects that need thumbnail fixes")
        
        success_count = 0
        for project in projects:
            if fix_project_thumbnail(project.id):
                success_count += 1
        
        print(f"🎉 Done! Fixed thumbnails for {success_count}/{len(projects)} projects")
        return True
        
    except Exception as e:
        print(f"❌ Error while fixing all project thumbnails: {e}")
        return False
    finally:
        db.close()

def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 修复指定项目
        project_id = sys.argv[1]
        print(f"🚀 Starting thumbnail fix for project {project_id}...")
        if fix_project_thumbnail(project_id):
            print("🎉 Thumbnail fix complete!")
        else:
            print("❌ Thumbnail fix failed")
            sys.exit(1)
    else:
        # 修复所有项目
        print("🚀 Starting thumbnail fix for all projects...")
        if fix_all_project_thumbnails():
            print("🎉 All thumbnails fixed!")
        else:
            print("❌ Thumbnail fix failed")
            sys.exit(1)

if __name__ == "__main__":
    main()
