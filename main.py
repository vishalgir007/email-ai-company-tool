# main.py
# Email → Company/Sector Identification Tool
# Enhanced with comprehensive error recovery and monitoring

import pandas as pd
import tldextract
from rapidfuzz import process, fuzz
import os
import argparse
import logging
from searcher import search_domain

# Import error recovery system
try:
    from error_recovery import (
        metrics_collector, print_metrics_summary, health_checker,
        logger, setup_monitoring
    )
    HAS_ERROR_RECOVERY = True
except ImportError:
    # Fallback if error_recovery module not available
    HAS_ERROR_RECOVERY = False
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    def print_metrics_summary():
        pass

# Base directory of the script (used for default CLI paths)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_domain(email):
    """Extract domain from email address"""
    ext = tldextract.extract(email)
    return ext.domain.lower()

def resolve_unresolved_domains(domains, workers=5, min_delay=0.5):
    """Resolve a set of domains using concurrent web lookups"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        from tqdm import tqdm
    except ImportError:
        # Fallback if tqdm not available
        def tqdm(iterable, **kwargs):
            return iterable

    results = {}
    domains = list(domains)
    if not domains:
        return results

    # Use ThreadPoolExecutor to run searches concurrently
    def search_single_domain(domain):
        return domain, search_domain(domain, pause=0.0, min_delay=min_delay)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(search_single_domain, domain): domain for domain in domains}
        for f in tqdm(as_completed(futures), total=len(futures), desc='Resolving domains'):
            try:
                domain, (company, sector) = f.result()
                results[domain] = (company, sector)
            except Exception as e:
                domain = futures[f]
                logger.warning(f"Failed to resolve {domain}: {e}")
                results[domain] = ('Unknown', 'Unknown')

    return results

def process_emails(emails_df, dataset_file=None, use_web=True, workers=5, threshold=80, use_async=False, min_delay=0.5):
    """
    Process emails to identify companies and sectors
    
    Args:
        emails_df: DataFrame with 'email' column
        dataset_file: Path to company dataset CSV
        use_web: Whether to use web search for unknown companies
        workers: Number of concurrent workers for web search
        threshold: Fuzzy matching threshold (0-100)
        use_async: Whether to use async processing
        min_delay: Minimum delay between web requests
    
    Returns:
        DataFrame with email, domain, company, sector columns
    """
    if HAS_ERROR_RECOVERY:
        logger.info(f"Starting email processing: {len(emails_df)} emails, use_web={use_web}, use_async={use_async}")
        setup_monitoring()
    
    # Extract domains
    emails_df = emails_df.copy()
    emails_df["domain"] = emails_df["email"].apply(get_domain)
    
    # Load dataset
    if dataset_file is None:
        dataset_file = os.path.join(BASE_DIR, 'Dataset', 'company_sector.csv')
        # Fallback to enhanced dataset if company_sector.csv doesn't exist
        if not os.path.exists(dataset_file):
            dataset_file = os.path.join(BASE_DIR, 'Dataset', 'enhanced_company_sector.csv')
    
    if not os.path.exists(dataset_file):
        raise FileNotFoundError(f"Dataset file not found: {dataset_file}")
    mapping_df = pd.read_csv(dataset_file)
    
    if HAS_ERROR_RECOVERY:
        logger.info(f"Loaded dataset with {len(mapping_df)} mappings")
    
    # Enhanced matching function with improved algorithms
    def match_company(domain):
        domain_lower = domain.lower().strip()
        
        # First try exact match
        exact_matches = mapping_df[mapping_df["domain"] == domain_lower]
        if not exact_matches.empty:
            matched_row = exact_matches.iloc[0]
            return pd.Series([matched_row["company"], matched_row["sector"]])
        
        # Try exact match with .com extension
        domain_with_com = f"{domain_lower}.com"
        exact_matches_com = mapping_df[mapping_df["domain"] == domain_with_com]
        if not exact_matches_com.empty:
            matched_row = exact_matches_com.iloc[0]
            return pd.Series([matched_row["company"], matched_row["sector"]])
        
        # Try removing common extensions and matching
        for ext in ['.com', '.net', '.org', '.io', '.co', '.tech', '.biz', '.ai']:
            if domain_lower.endswith(ext):
                base_domain = domain_lower[:-len(ext)]
                base_matches = mapping_df[mapping_df["domain"] == base_domain]
                if not base_matches.empty:
                    matched_row = base_matches.iloc[0]
                    return pd.Series([matched_row["company"], matched_row["sector"]])
        
        # Try partial matching (contains)
        for _, row in mapping_df.iterrows():
            domain_key = row["domain"].lower()
            if domain_lower in domain_key or domain_key in domain_lower:
                if abs(len(domain_lower) - len(domain_key)) <= 3:  # Similar length
                    return pd.Series([row["company"], row["sector"]])
        
        # Try enhanced fuzzy matching with multiple algorithms and lower threshold
        choices = mapping_df["domain"].tolist()
        best_matches = [
            process.extractOne(domain_lower, choices, scorer=fuzz.ratio),
            process.extractOne(domain_lower, choices, scorer=fuzz.partial_ratio),
            process.extractOne(domain_lower, choices, scorer=fuzz.token_sort_ratio),
            process.extractOne(domain_lower, choices, scorer=fuzz.token_set_ratio)
        ]
        
        # Use the best match with score >= 65% (lowered significantly)
        for match in best_matches:
            if match and match[1] >= 65:
                matched_row = mapping_df[mapping_df["domain"] == match[0]].iloc[0]
                return pd.Series([matched_row["company"], matched_row["sector"]])
        
        # Try matching cleaned domain (remove numbers and suffixes)
        import re
        cleaned_domain = re.sub(r'\d+', '', domain_lower)
        suffixes = ['corp', 'inc', 'llc', 'ltd', 'company', 'co', 'group', 'international', 'intl', 'global', 'usa', 'us', 'solutions', 'services', 'systems', 'tech', 'technologies']
        for suffix in suffixes:
            if cleaned_domain.endswith(suffix):
                cleaned_domain = cleaned_domain[:-len(suffix)]
                break
        
        if cleaned_domain and cleaned_domain != domain_lower:
            cleaned_matches = mapping_df[mapping_df["domain"] == cleaned_domain]
            if not cleaned_matches.empty:
                matched_row = cleaned_matches.iloc[0]
                return pd.Series([matched_row["company"], matched_row["sector"]])
        
        # Last resort: generate company name and sector from domain
        if not use_web:
            company_name = domain_lower.replace('.', ' ').replace('-', ' ').replace('_', ' ')
            words = company_name.split()
            if words:
                meaningful_words = [word.capitalize() for word in words 
                                 if len(word) > 2 and word.lower() not in ['com', 'net', 'org', 'www', 'mail', 'email']]
                if meaningful_words:
                    company_name = ' '.join(meaningful_words)
                    # Smart sector assignment
                    sector = 'Unknown'
                    if any(tech_word in domain_lower for tech_word in ['tech', 'soft', 'system', 'data', 'digital', 'cyber', 'cloud', 'ai', 'ml']):
                        sector = 'Technology'
                    elif any(fin_word in domain_lower for fin_word in ['bank', 'financial', 'finance', 'capital', 'invest', 'fund', 'money']):
                        sector = 'Finance'
                    elif any(health_word in domain_lower for health_word in ['health', 'medical', 'pharma', 'bio', 'care', 'hospital', 'clinic']):
                        sector = 'Healthcare'
                    elif any(retail_word in domain_lower for retail_word in ['shop', 'store', 'retail', 'market', 'buy', 'sell']):
                        sector = 'Retail'
                    elif any(mfg_word in domain_lower for mfg_word in ['manufacturing', 'industrial', 'factory', 'production', 'auto', 'motor']):
                        sector = 'Manufacturing'
                    elif any(service_word in domain_lower for service_word in ['consulting', 'advisory', 'service', 'solution']):
                        sector = 'Consulting'
                    
                    return pd.Series([company_name, sector])
            return pd.Series(["Unknown", "Unknown"])
        
        return pd.Series(["__UNRESOLVED__", "__UNRESOLVED__"])
    
    # Apply fuzzy matching
    emails_df[["company", "sector"]] = emails_df["domain"].apply(match_company)
    
    # Handle unresolved domains with web search
    unresolved = set(emails_df.loc[emails_df['company'] == '__UNRESOLVED__', 'domain'].unique())
    
    if use_web and unresolved:
        resolved_map = resolve_unresolved_domains(unresolved, workers=workers, min_delay=min_delay)
        
        def apply_resolved(row):
            if row['company'] != '__UNRESOLVED__':
                return (row['company'], row['sector'])
            d = row['domain']
            if d in resolved_map:
                return resolved_map[d]
            else:
                return ('Unknown', 'Unknown')
        
        new_cols = emails_df.apply(apply_resolved, axis=1, result_type='expand')
        emails_df[['company', 'sector']] = new_cols.iloc[:, 0:2].values
    else:
        # Replace __UNRESOLVED__ with Unknown
        emails_df.loc[emails_df['company'] == '__UNRESOLVED__', ['company', 'sector']] = ['Unknown', 'Unknown']
    
    if HAS_ERROR_RECOVERY:
        # Save results  
        output_path = os.path.join(BASE_DIR, 'Output', 'results.xlsx')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        emails_df.to_excel(output_path, index=False)
        logger.info(f"Results saved to {output_path}")
        logger.info(f"Email processing completed successfully: {len(emails_df)} emails processed")
    
    return emails_df

def parse_args():
    """Parse command line arguments"""
    p = argparse.ArgumentParser(description='Email → Company/Sector Identification Tool')
    p.add_argument('--input', '-i', default=os.path.join(BASE_DIR, 'Input', 'emails.csv'))
    p.add_argument('--dataset', '-d', default=os.path.join(BASE_DIR, 'Dataset', 'enhanced_company_sector.csv'))
    p.add_argument('--output', '-o', default=os.path.join(BASE_DIR, 'Output', 'results.xlsx'))
    p.add_argument('--threshold', '-t', type=int, default=80, help='fuzzy match threshold (0-100)')
    p.add_argument('--no-web', dest='use_web', action='store_false', help='disable web fallback for unknown domains')
    p.add_argument('--workers', '-w', type=int, default=5, help='number of concurrent web lookup workers')
    p.add_argument('--min-delay', type=float, default=0.5, help='minimum seconds between requests to the same host')
    p.add_argument('--async-run', dest='use_async', action='store_true', help='use async resolver (aiohttp) for web lookups')
    p.set_defaults(use_async=False)
    p.set_defaults(use_web=True)
    p.add_argument('--migrate-cache', action='store_true', help='import Dataset/search_cache.csv into the SQLite cache and exit')
    p.add_argument('--clean-csv', action='store_true', help='clean duplicate rows from Dataset/search_cache.csv (backup created) and exit')
    p.add_argument('--use-wikidata', action='store_true', help='try Wikidata lookup for unresolved companies')
    return p.parse_args()

def main():
    """Main CLI execution function"""
    args = parse_args()

    # Handle special commands
    if args.clean_csv:
        try:
            from searcher import clean_csv_cache
            n = clean_csv_cache()
            print(f"Cleaned CSV cache, {n} unique rows written to Dataset/search_cache.csv")
        except Exception as e:
            print('Error cleaning CSV cache:', e)
        raise SystemExit(0)
    
    if args.migrate_cache:
        try:
            from searcher import import_csv_to_db
            n = import_csv_to_db()
            print(f"Migrated {n} rows from CSV to SQLite cache")
        except Exception as e:
            print('Error migrating cache:', e)
        raise SystemExit(0)

    # Load input file
    input_file = args.input
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
    emails_df = pd.read_csv(input_file)
    
    # Process emails
    result_df = process_emails(
        emails_df, 
        dataset_file=args.dataset,
        use_web=args.use_web,
        workers=args.workers,
        threshold=args.threshold,
        use_async=args.use_async,
        min_delay=args.min_delay
    )
    
    # Save results
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    result_df.to_excel(args.output, index=False)
    
    if HAS_ERROR_RECOVERY:
        logger.info(f"Results saved to {args.output}")
        logger.info(f"Email processing completed successfully: {len(result_df)} emails processed")
        print_metrics_summary()
    
    print(f"✅ Done! Results saved to '{args.output}'")
    print(result_df)
    print_metrics_summary()

# Only execute main when run as script
if __name__ == '__main__':
    main()