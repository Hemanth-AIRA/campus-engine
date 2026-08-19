#!/usr/bin/env python3
"""
Database Migration Script - Create interview_timeline table
Run this script to add the new table to your database
"""

import os
import sys
from dotenv import load_dotenv
import pymysql
import logging as _lg; logger = _lg.getLogger("run_migration")

# Load environment variables
load_dotenv(override=True)

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB = os.getenv("MYSQL_DB")

def run_migration():
    """Execute the migration SQL file"""
    try:
        # Connect to database
        connection = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASS,
            database=MYSQL_DB
        )
        
        logger.info("=" * 60)
        logger.info("📊 DATABASE MIGRATION: Create interview_timeline table")
        logger.info("=" * 60)
        logger.info(f"Connected to database: {MYSQL_DB} at {MYSQL_HOST}:{MYSQL_PORT}")
        
        # Read migration SQL file
        migration_file = "migrations/001_create_interview_timeline_table.sql"
        with open(migration_file, 'r') as f:
            sql = f.read()
        
        # Execute migration
        with connection.cursor() as cursor:
            # Split by semicolon to handle multiple statements
            statements = [s.strip() for s in sql.split(';') if s.strip() and not s.strip().startswith('--')]
            
            for i, statement in enumerate(statements, 1):
                logger.info(f"Executing statement {i}/{len(statements)}...")
                cursor.execute(statement)
                logger.info(f"✅ Statement {i} executed successfully")
            
            connection.commit()
        
        logger.info("=" * 60)
        logger.info("✅ MIGRATION COMPLETED SUCCESSFULLY!")
        logger.info("=" * 60)
        logger.info("Table 'interview_timeline' created with the following structure:")
        logger.info("  - id (BIGINT, AUTO_INCREMENT)")
        logger.info("  - interview_id (BIGINT)")
        logger.info("  - timestamp (DATETIME)")
        logger.info("  - conversation_type (VARCHAR(50))")
        logger.info("  - aira_said (TEXT)")
        logger.info("  - candidate_said (TEXT)")
        logger.info("  - question_index (BIGINT, NULL)")
        logger.info("  - flag (BOOLEAN)")
        logger.info("  - sequence_number (BIGINT)")
        logger.info("  - created_at (TIMESTAMP)")
        logger.info("=" * 60)
        
        # Verify table was created
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE 'interview_timeline'")
            result = cursor.fetchone()
            if result:
                logger.info("✅ Verification: Table 'interview_timeline' exists")
                
                # Show table structure
                cursor.execute("DESCRIBE interview_timeline")
                columns = cursor.fetchall()
                logger.info("\n📋 Table Structure:")
                for col in columns:
                    logger.info(f"  {col}")
            else:
                logger.error("❌ Verification failed: Table not found!")
                return False
        
        connection.close()
        logger.info("\n🎉 Migration completed! You can now start tracking conversation timelines.")
        return True
        
    except FileNotFoundError:
        logger.error(f"❌ Migration file not found: {migration_file}")
        logger.error("Please ensure the migrations directory exists with the SQL file.")
        return False
    except pymysql.Error as e:
        logger.error(f"❌ Database error: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("AIRA Database Migration - Create interview_timeline Table")
    print("=" * 60)
    print("\nThis will create a new table to store complete conversation")
    print("timelines including all AIRA-candidate exchanges.")
    print("\nTable: interview_timeline")
    print("Purpose: Enable future report regeneration with full context")
    print("\n" + "=" * 60)
    
    response = input("\nProceed with migration? (yes/no): ").strip().lower()
    
    if response in ['yes', 'y']:
        success = run_migration()
        sys.exit(0 if success else 1)
    else:
        print("❌ Migration cancelled by user.")
        sys.exit(1)
