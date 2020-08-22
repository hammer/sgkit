import dask.array as da
import xarray as xr

from sgkit.typing import ArrayLike


def gramian(a: ArrayLike) -> ArrayLike:
    """Returns gramian matrix of the given matrix"""
    return a.T.dot(a)


def pc_relate(ds: xr.Dataset, maf: float = 0.01) -> xr.Dataset:
    """Compute PC-Relate as described in Conomos, et al. 2016 [1].

    Parameters
    ----------
    ds : `xr.Dataset`
        Dataset containing (S = num samples, V = num variants, D = ploidy, PC = num PC):
        * genotype calls: "call_genotype" (SxVxD)
        * genotype calls mask: "call_genotype_mask" (SxVxD)
        * sample PCs: "sample_pcs" (PCxS)
    maf : float
        individual minor allele frequency filter. If an individual's estimated
        individual-specific minor allele frequency at a SNP is less than this value,
        that SNP will be excluded from the analysis for that individual.
        The default value is 0.01. Must be between (0.0, 0.1).

    Warnings
    --------
    This function is only applicable to diploid, biallelic datasets.

    Returns
    -------
    Dataset
        Dataset containing (S = num samples):
        pc_relate_phi: (S,S) ArrayLike
            pairwise recent kinship estimation matrix as float in [-0.5, 0.5].

    References
    ----------
    - [1] Conomos MP, Reiner AP, Weir BS & Thornton TA. 2016.
        "Model-free Estimation of Recent Genetic Relatedness."
        Am. J. Hum. Genet 98, 127–148.

    Raises
    ------
    ValueError
        * If ploidy of provided dataset != 2
        * If maximum number of alleles in provided dataset != 2
        * Input dataset is missing any of the required variables
        * If maf is not in (0.0, 1.0)
    """
    if maf <= 0.0 or maf >= 1.0:
        raise ValueError("MAF must be between (0.0, 1.0)")
    if "ploidy" in ds.dims and ds.dims["ploidy"] != 2:
        raise ValueError("PC Relate only work for diploid genotypes")
    if "alleles" in ds.dims and ds.dims["alleles"] != 2:
        raise ValueError("PC Relate only work for biallelic genotypes")
    if "call_genotype" not in ds:
        raise ValueError("Input dataset must contain call_genotype")
    if "call_genotype_mask" not in ds:
        raise ValueError("Input dataset must contain call_genotype_mask")
    if "sample_pcs" not in ds:
        raise ValueError("Input dataset must contain sample_pcs variable")

    call_g_mask = ds["call_genotype_mask"].any(dim="ploidy")
    call_g = xr.where(call_g_mask, -1, ds["call_genotype"].sum(dim="ploidy"))  # type: ignore[no-untyped-call]

    # impute with variant mean
    variant_mean = (
        call_g.where(~call_g_mask)
        .mean(dim="samples")
        .expand_dims(dim="samples", axis=1)
    )
    imputed_g = da.where(call_g_mask, variant_mean, call_g)

    # 𝔼[gs|V] = 1β0 + Vβ, where 1 is a length _s_ vector of 1s, and β = (β1,...,βD)^T
    # is a length D vector of regression coefficients for each of the PCs
    pcs = ds["sample_pcs"]
    pcsi = da.concatenate([da.ones((1, pcs.shape[1]), dtype=pcs.dtype), pcs], axis=0)
    # Note: dask qr decomp requires no chunking in one dimension, and because number of
    # components should be smaller than number of samples in most cases, we disable
    # chunking on number components
    pcsi = pcsi.T.rechunk((None, -1))

    q, r = da.linalg.qr(pcsi)
    # mu, eq: 3
    half_beta = da.linalg.inv(2 * r).dot(q.T).dot(imputed_g.T)
    mu = pcsi.dot(half_beta).T
    # phi, eq: 4
    mask = (mu <= maf) | (mu >= 1.0 - maf) | call_g_mask
    mu_mask = da.ma.masked_array(mu, mask=mask)
    variance = mu_mask.map_blocks(lambda i: i * (1.0 - i))
    variance = da.ma.filled(variance, fill_value=0.0)
    stddev = da.sqrt(variance)
    centered_af = call_g / 2 - mu_mask
    centered_af = da.ma.filled(centered_af, fill_value=0.0)
    # NOTE: gramian could be a performance bottleneck, and we could explore
    #       performance improvements like (or maybe sth else):
    #       * calculating only the pairs we are interested in
    #       * using an optimized einsum.
    phi = gramian(centered_af) / gramian(stddev)
    return xr.Dataset({"pc_relate_phi": (("sample_x", "sample_y"), phi)})
