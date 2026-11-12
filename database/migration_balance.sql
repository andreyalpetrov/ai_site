-- Выполнить один раз в базе sch688_vvedenie.
-- Скрипт добавляет users.balance, только если такого столбца ещё нет.

SET @has_balance = (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'balance'
);
SET @sql = IF(
  @has_balance = 0,
  'ALTER TABLE `users` ADD COLUMN `balance` DECIMAL(12,2) NOT NULL DEFAULT 0.00',
  'SELECT "users.balance already exists"'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS `transactions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `amount` DECIMAL(12,2) NOT NULL,
  `type` VARCHAR(32) NOT NULL,
  `cost` DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  `description` VARCHAR(255) NOT NULL DEFAULT '',
  `balance_after` DECIMAL(12,2) NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_transactions_user_created` (`user_id`, `created_at`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
