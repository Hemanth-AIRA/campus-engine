# AIRA Database Management

This directory contains all database-related files for the AIRA system, including schema definitions, migrations, and database management utilities.

## 📁 Directory Structure

```
database/
├── README.md                    # This file
├── schema.sql                   # Main database schema
├── schema_updates.sql           # Additional schema updates
└── migrations/                  # Database migrations
    ├── 001_add_mandatory_skills_migration.sql     # SQL migration for mandatory skills
    └── 001_run_mandatory_skills_migration.py      # Python migration runner
```

## 🗄️ Database Schema Files

### `schema.sql`

The main AIRA database schema containing:

- `jobs` table - Job descriptions and requirements from ATS
- `interviews` table - Interview sessions and results
- `candidates` table - Candidate information
- Indexes and constraints for optimal performance

### `schema_updates.sql`

Additional schema modifications and updates made to the original schema.

## 🚀 Database Migrations

Database migrations are versioned and stored in the `migrations/` directory. Each migration is numbered sequentially for proper ordering.

### Migration Naming Convention

```
{version}_{descriptive_name}_migration.{ext}
```

Example:

- `001_add_mandatory_skills_migration.sql` - SQL migration script
- `001_run_mandatory_skills_migration.py` - Python migration runner

### Running Migrations

#### Option 1: SQL Migration (Direct)

```bash
# Run SQL migration directly
mysql -u username -p database_name < database/migrations/001_add_mandatory_skills_migration.sql
```

#### Option 2: Python Migration Runner (Recommended)

```bash
# Configure Python environment first
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install pymysql python-dotenv

# Run Python migration with error handling
cd database/migrations
python 001_run_mandatory_skills_migration.py
```

### Migration 001: Mandatory Skills Support

**Purpose**: Adds mandatory skills functionality to the AIRA system

**Changes**:

- Adds `mandatory_skills` (LONGTEXT) column to `jobs` table
- Adds `has_mandatory_skills` (BOOLEAN) column to `jobs` table
- Adds `mandatory_skills` (LONGTEXT) column to `interviews` table
- Adds `has_mandatory_skills` (BOOLEAN) column to `interviews` table
- Creates performance indexes for mandatory skills queries
- Populates existing records with mandatory skills data using regex extraction

**Files**:

- `001_add_mandatory_skills_migration.sql` - SQL DDL statements
- `001_run_mandatory_skills_migration.py` - Python runner with error handling

## 🔧 Prerequisites

### Environment Variables

Create a `.env` file in the project root with:

```env
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_username
MYSQL_PASS=your_password
MYSQL_DB=your_database_name
```

### Python Dependencies

```bash
pip install pymysql python-dotenv
```

## 📋 Migration Checklist

Before running any migration:

1. **Backup Database**

   ```bash
   mysqldump -u username -p database_name > backup_$(date +%Y%m%d_%H%M%S).sql
   ```

2. **Test on Development Environment**

   - Run migration on a copy of production data
   - Verify all changes work as expected

3. **Review Migration Script**

   - Check for conditional DDL statements
   - Ensure backward compatibility
   - Verify index creation

4. **Run Migration**

   - Use Python runner for better error handling
   - Monitor execution logs
   - Verify schema changes

5. **Post-Migration Verification**

   ```sql
   -- Check new columns exist
   SHOW COLUMNS FROM jobs WHERE Field LIKE '%mandatory%';
   SHOW COLUMNS FROM interviews WHERE Field LIKE '%mandatory%';

   -- Check indexes were created
   SHOW INDEX FROM jobs WHERE Key_name LIKE '%mandatory%';
   SHOW INDEX FROM interviews WHERE Key_name LIKE '%mandatory%';
   ```

## 🔍 Troubleshooting

### Common Issues

1. **Missing Dependencies**

   ```bash
   pip install pymysql python-dotenv
   ```

2. **Environment Variables Not Loaded**

   - Ensure `.env` file exists in project root
   - Check variable names match exactly

3. **Permission Issues**

   - Ensure database user has ALTER, CREATE, INSERT privileges
   - Check connection parameters

4. **Column Already Exists**
   - Migration scripts include conditional DDL
   - Safe to re-run migrations

### Verification Commands

```sql
-- Check mandatory skills columns
SELECT
    TABLE_NAME,
    COLUMN_NAME,
    DATA_TYPE,
    IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'your_database'
AND COLUMN_NAME LIKE '%mandatory%';

-- Check indexes
SELECT
    TABLE_NAME,
    INDEX_NAME,
    COLUMN_NAME
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = 'your_database'
AND INDEX_NAME LIKE '%mandatory%';
```

## 📚 Related Documentation

- [MANDATORY_SKILLS_MIGRATION.md](../MANDATORY_SKILLS_MIGRATION.md) - Detailed migration documentation
- [README.md](../README.md) - Main project documentation
- [aira_sql.py](../aira_sql.py) - SQLAlchemy models for database access

## 🚨 Important Notes

- Always backup your database before running migrations
- Test migrations on development environment first
- Migration scripts are designed to be idempotent (safe to run multiple times)
- Follow the migration numbering convention for new migrations
- Document all schema changes in this README
