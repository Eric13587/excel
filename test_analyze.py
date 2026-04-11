import sqlite3
import pandas as pd

conn = sqlite3.connect('loan_master.db')
df = pd.read_sql_query("SELECT id, individual_id, date, amount, notes, batch_id FROM savings WHERE notes='Monthly Increment (Auto)';", conn)
print("Affected batches and amounts:")
print(df.groupby(['batch_id', 'amount']).size().reset_index(name='count'))
for batch in df['batch_id'].dropna().unique():
    print(f"\nBatch ID: {batch}")
    b_df = df[df['batch_id'] == batch]
    print(b_df.head(10))
