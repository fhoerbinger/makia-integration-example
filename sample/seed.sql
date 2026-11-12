-- Invented sample data: two engagements, their team, and two records.
INSERT INTO src_projects (external_id, name, archived) VALUES ('2026-114', 'Engagement 2026-114', 0);
INSERT INTO src_projects (external_id, name, archived) VALUES ('2025-087', 'Engagement 2025-087', 1);
INSERT INTO src_members (project_external_id, subject, permissions) VALUES ('2026-114', 'uid=a.huber,ou=people', 'review,cases');
INSERT INTO src_members (project_external_id, subject, permissions) VALUES ('2026-114', 'uid=m.berger,ou=people', 'review');
INSERT INTO src_members (project_external_id, subject, permissions) VALUES ('2025-087', 'uid=a.huber,ou=people', 'review');
INSERT INTO src_records (external_id, project_external_id, workflow_slug, input_id) VALUES ('DOC-77812', '2026-114', 'invoice-check', 'invoices');
INSERT INTO src_records (external_id, project_external_id, workflow_slug, input_id) VALUES ('DOC-77813', '2026-114', 'invoice-check', 'invoices');
INSERT INTO src_record_files (record_external_id, path) VALUES ('DOC-77812', '2026/114/invoice-1.pdf');
INSERT INTO src_record_files (record_external_id, path) VALUES ('DOC-77812', '2026/114/invoice-2.pdf');
INSERT INTO src_record_files (record_external_id, path) VALUES ('DOC-77813', '2026/114/invoice-3.pdf');
