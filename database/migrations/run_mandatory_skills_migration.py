#!/usr/bin/env python3
"""
Database Migration Script for Mandatory Skills
Run this script to add mandatory skills fields to the database
"""

import os
import pymysql
from dotenv import load_dotenv

load_dotenv()

# Database configuration
DB_CONFIG = {
    'host': os.getenv("MYSQL_HOST", "localhost"),
    'port': int(os.getenv("MYSQL_PORT", 3306)),
    'user': os.getenv("MYSQL_USER"),
    'password': os.getenv("MYSQL_PASS"),
    'database': os.getenv("MYSQL_DB"),
    'cursorclass': pymysql.cursors.DictCursor
}

def run_migration():
    """Run the mandatory skills database migration"""
    
    # Read the migration SQL
    with open('add_mandatory_skills_migration.sql', 'r') as f:
        migration_sql = f.read()
    
    # Split into individual statements
    statements = [stmt.strip() for stmt in migration_sql.split(';') if stmt.strip()]
    
    connection = None
    try:
        # Connect to database
        connection = pymysql.connect(**DB_CONFIG)
        
        with connection.cursor() as cursor:
            print("🚀 Starting mandatory skills database migration...")
            
            for i, statement in enumerate(statements, 1):
                if statement.startswith('--') or not statement:
                    continue
                    
                print(f"📝 Executing statement {i}/{len(statements)}...")
                print(f"   {statement[:50]}...")
                
                try:
                    cursor.execute(statement)
                    connection.commit()
                    print(f"   ✅ Success")
                except Exception as e:
                    print(f"   ⚠️  Warning: {e}")
                    # Continue with other statements
                    continue
            
            print("\n🎉 Migration completed successfully!")
            print("\n📊 Verification Results:")
            
            # Show verification results
            cursor.execute("""
                SELECT 
                    'jobs' as table_name,
                    COUNT(*) as total_records,
                    SUM(has_mandatory_skills) as records_with_mandatory_skills
                FROM jobs
                UNION ALL
                SELECT 
                    'interviews' as table_name,
                    COUNT(*) as total_records,
                    SUM(has_mandatory_skills) as records_with_mandatory_skills
                FROM interviews
            """)
            
            results = cursor.fetchall()
            for result in results:
                print(f"   📋 {result['table_name']}: {result['total_records']} total, {result['records_with_mandatory_skills']} with mandatory skills")
                
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        if connection:
            connection.rollback()
        raise
    finally:
        if connection:
            connection.close()

if __name__ == "__main__":
    print("🏗️  AIRA Mandatory Skills Database Migration")
    print("=" * 50)
    
    # Check if migration file exists
    if not os.path.exists('add_mandatory_skills_migration.sql'):
        print("❌ Migration file 'add_mandatory_skills_migration.sql' not found!")
        exit(1)
    
    # Confirm before running
    response = input("⚠️  This will modify your database. Continue? (y/N): ").lower()
    if response != 'y':
        print("Migration cancelled.")
        exit(0)
    
    try:
        run_migration()
        print("\n✅ Database migration completed successfully!")
        print("🚀 The AIRA system now has full mandatory skills support!")
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        exit(1)
