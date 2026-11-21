#!/usr/bin/env python3
"""
为现有项目生成缩略图的脚本
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
from sqlalchemy import text

def generate_thumbnails_for_projects():
    """为所有没有缩略图的项目生成缩略图"""
    db = SessionLocal()
    try:
        # 查找所有没有缩略图但有视频文件的项目
        projects = db.query(Project).filter(
            Project.thumbnail.is_(None),
            Project.video_path.isnot(None)
        ).all()
        
        if not projects:
            print("✅ All projects already have thumbnails")
            return True
        
        print(f"📋 Found {len(projects)} project(s) that need thumbnails")
        
        success_count = 0
        for project in projects:
            try:
                print(f"🎬 Generating thumbnail for project '{project.name}' ({project.id})...")
                
                # 检查视频文件是否存在
                video_path = Path(project.video_path)
                if not video_path.exists():
                    print(f"⚠️  Video file does not exist: {video_path}")
                    continue
                
                # 生成缩略图
                thumbnail_data = generate_project_thumbnail(project.id, video_path)
                
                if thumbnail_data:
                    # 保存到数据库
                    project.thumbnail = thumbnail_data
                    db.commit()
                    print(f"✅ Thumbnail generated for project '{project.name}'")
                    success_count += 1
                else:
                    print(f"❌ Failed to generate thumbnail for project '{project.name}'")
                    
            except Exception as e:
                print(f"❌ Failed to process project '{project.name}': {e}")
                db.rollback()
                continue
        
        print(f"🎉 Done! Generated thumbnails for {success_count}/{len(projects)} project(s)")
        return True
        
    except Exception as e:
        print(f"❌ Error while generating thumbnails: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def generate_thumbnail_for_project(project_id: str):
    """为指定项目生成缩略图"""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        
        if not project:
            print(f"❌ Project {project_id} does not exist")
            return False
        
        if not project.video_path:
            print(f"❌ Project {project_id} has no video file")
            return False
        
        # 检查视频文件是否存在
        video_path = Path(project.video_path)
        if not video_path.exists():
            print(f"❌ Video file does not exist: {video_path}")
            return False
        
        print(f"🎬 Generating thumbnail for project '{project.name}' ({project.id})...")
        
        # 生成缩略图
        thumbnail_data = generate_project_thumbnail(project.id, video_path)
        
        if thumbnail_data:
            # 保存到数据库
            project.thumbnail = thumbnail_data
            db.commit()
            print(f"✅ Thumbnail generated for project '{project.name}'")
            return True
        else:
            print(f"❌ Failed to generate thumbnail for project '{project.name}'")
            return False
            
    except Exception as e:
        print(f"❌ Error while processing project {project_id}: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 为指定项目生成缩略图
        project_id = sys.argv[1]
        print(f"🚀 Generating thumbnail for project {project_id}...")
        if generate_thumbnail_for_project(project_id):
            print("🎉 Thumbnail generation complete!")
        else:
            print("❌ Failed to generate thumbnail")
            sys.exit(1)
    else:
        # 为所有项目生成缩略图
        print("🚀 Generating thumbnails for all projects...")
        if generate_thumbnails_for_projects():
            print("🎉 All thumbnails generated!")
        else:
            print("❌ Failed to generate thumbnail")
            sys.exit(1)

if __name__ == "__main__":
    main()
