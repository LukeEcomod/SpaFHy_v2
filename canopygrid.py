# -*- coding: utf-8 -*-
"""
Created on Fri Mar 24 11:01:50 2017

@author: slauniai

******************************************************************************
CanopyGrid:

Gridded canopy and snow hydrology model for SpaFHy -integration
Based on simple schemes for computing water flows and storages within vegetation
canopy and snowpack at daily or sub-daily timesteps.

(C) Samuli Launiainen, 2016-

******************************************************************************

Modified by khaahti, jpnousu

"""
import numpy as np
import configparser
eps = np.finfo(float).eps

class CanopyGrid():
    def __init__(self, cpara, state):
        """
        initializes CanopyGrid -object

        Args:
            cpara - parameter dict:
            state - dict of initial state
            outputs - True saves output grids to list at each timestep

        Returns:
            self - object

        NOTE:
            Currently the initialization assumes simulation start 1st Jan,
            and sets self._LAI_decid and self.X equal to minimum values.
            Also leaf-growth & senescence parameters are intialized to zero.
        """
        epsi = 0.01

        cmask = state['hc'].copy()
        cmask[np.isfinite(cmask)] = 1.0
        self.cmask = cmask

        self.latitude = cpara['loc']['lat'] * cmask
        self.longitude = cpara['loc']['lon']

        # physiology: transpi + floor evap
        self.physpara = cpara['physpara']

        # phenology
        self.phenopara = cpara['phenopara']
        #spec_para = cpara['spec_para']

        # canopy parameters and state
        self.hc = state['hc'] + eps
        self.cf = state['cf'] + eps

        '''
        state['lai_decid'] = state['lai_decid_max']

        ptypes = {}
        LAI = 0.0

        for pt in list(spec_para.keys()):
            ptypes[pt] = spec_para[pt]
            ptypes[pt]['LAImax'] = state['lai_' + pt]

        self.ptypes = ptypes

        # compute gridcell average LAI and photosynthesis-stomatal conductance parameters:
        LAI = 0.0
        Amax = 0.0
        q50 = 0.0
        g1 = 0.0
        for pt in self.ptypes.keys():
            if self.ptypes[pt]['lai_cycle']:
                pt_lai = self.ptypes[pt]['LAImax'] * self.phenopara['lai_decid_min']
            else:
                pt_lai = self.ptypes[pt]['LAImax']

            LAI += pt_lai
            Amax += pt_lai * ptypes[pt]['amax']
            q50 += pt_lai * ptypes[pt]['q50']
            g1 += pt_lai * ptypes[pt]['g1']

        self.LAI = LAI + eps
        self.physpara.update({'Amax': Amax / self.LAI, 'q50': q50 / self.LAI, 'g1': g1 / self.LAI})

        del Amax, q50, g1, pt, LAI, pt_lai
        '''

        self._LAIconif = np.maximum(state['lai_conif'], eps)  # m2m-2
        self._LAIdecid = state['lai_decid_max'] * self.phenopara['lai_decid_min']
        self._LAIgrass_max = state['lai_grass']
        self._LAIgrass = state['lai_grass'] * self.phenopara['lai_decid_min']
        #print(self._LAIgrass[60,60])
        self._LAIshrub = np.maximum(state['lai_shrub'], eps)

        self.LAI = self._LAIconif + self._LAIdecid #+ self._LAIshrub + self._LAIgrass

        self._LAIdecid_max = state['lai_decid_max']  # m2m-2

        # senescence starts at first doy when daylength < self.phenopara['sdl']
        self.phenopara['sso'] = np.ones(np.shape(self.latitude))*np.nan
        doy = np.arange(1, 366)
        for lat in np.unique(self.latitude):
            if np.isnan(lat):
                break
            # senescence starts at first doy when daylength < self.phenopara['sdl']
            dl = daylength(lat, doy)
            ix = np.max(np.where(dl > self.phenopara['sdl']))
            self.phenopara['sso'][self.latitude == lat] = doy[ix]  # this is onset date for senescence
#            print(lat, doy[ix])
            del ix
        self.phenopara['sso'] = self.phenopara['sso'] * cmask

        # self.cpara = cpara  # added new parameters self.cpara['kmt'],
        # self.cpara['kmr'] here for testing radiation-based snow melt model
        self.wmax = cpara['interc']['wmax']
        self.wmaxsnow = cpara['interc']['wmaxsnow']
        self.Kmelt = cpara['snow']['kmelt']
        self.Kfreeze = cpara['snow']['kfreeze']
        self.R = cpara['snow']['r']  # max fraction of liquid water in snow

        # --- for computing aerodynamic resistances
        self.zmeas = cpara['flow']['zmeas']
        self.zground =cpara['flow']['zground'] # reference height above ground [m]
        self.zo_ground = cpara['flow']['zo_ground'] # ground roughness length [m]
        self.gsoil = self.physpara['gsoil']

        # --- state variables
        self.W = np.minimum(state['w'], self.wmax*self.LAI)
        self.SWE = state['swe']
        self.SWEi = self.SWE
        self.SWEl = np.zeros(np.shape(self.SWE))

        # deciduous leaf growth stage
        # NOTE: this assumes simulations start 1st Jan each year !!!
        self.DDsum = self.W * 0.0
        self.X = self.W * 0.0
        #self._relative_lai = self.phenopara['lai_decid_min']
        self._growth_stage = self.W * 0.0
        self._senesc_stage = self.W *0.0

    def run_timestep(self, doy, dt, Ta, Prec, Rg, Par, VPD, U=2.0, CO2=380.0, Rew=1.0, beta=1.0, P=101300.0):
        """
        Runs CanopyGrid instance for one timestep
        IN:
            doy - day of year
            dt - timestep [s]
            Ta - air temperature  [degC], scalar or (n x m) -matrix
            prec - precipitatation rate [mm/s]
            Rg - global radiation [Wm-2], scalar or matrix
            Par - photos. act. radiation [Wm-2], scalar or matrix
            VPD - vapor pressure deficit [kPa], scalar or matrix
            U - mean wind speed at ref. height above canopy top [ms-1], scalar or matrix
            CO2 - atm. CO2 mixing ratio [ppm]
            Rew - relative extractable water [-], scalar or matrix
            beta - term for soil evaporation resistance (Wliq/FC) [-]
            P - pressure [Pa], scalar or matrix
        OUT:
            updated CanopyGrid instance state variables
            flux grids PotInf, Trfall, Interc, Evap, ET, MBE [mm]
        """

        # Rn = 0.7 * Rg #net radiation
        Rn = np.maximum(2.57 * self.LAI / (2.57 * self.LAI + 0.57) - 0.2,
                        0.55) * Rg  # Launiainen et al. 2016 GCB, fit to Fig 2a

        # vpd limit
        #if VPD < 0.1:
        #    VPD = 0.1


        """ --- update phenology: self.ddsum & self.X ---"""
        #self.update_daily(Ta, doy)
        self._degreeDays(Ta, doy)
        fPheno = self._photoacclim(Ta)

        """ --- update deciduous leaf area index --- """
        laifract = self._lai_dynamics(doy)

        """ --- aerodynamic conductances --- """
        Ra, _, Ras, _, _, _ = aerodynamics(self.LAI, self.hc, U, w=0.01, zm=self.zmeas,
                                                  zg=self.zground, zos=self.zo_ground)

        """ --- interception, evaporation and snowpack --- """
        PotInf, Trfall, Evap, Interc, MBE, erate, unload, fact, Sfall, Rfall = self.canopy_water_snow(dt, Ta, Prec, Rn, VPD, Ra=Ra)

        """--- dry-canopy evapotranspiration [mm s-1] --- """
        Transpi, Efloor, Gc = self.dry_canopy_et(VPD, Par, Rn, Ta, Ra=Ra, Ras=Ras, CO2=CO2, Rew=Rew, beta=beta, fPheno=fPheno)

        Transpi = Transpi * dt
        Efloor = Efloor * dt
        #ET = Transpi + Efloor


        results = {
                'potential_infiltration': PotInf,  # [mm d-1]
                'interception': Interc,  # [mm d-1]
                'evaporation': Evap,  # [mm d-1]
                'forestfloor_evaporation': Efloor,  # [mm d-1]
                'transpiration': Transpi,  # [mm d-1]
                'throughfall': Trfall,  #[mm d-1]
                'snow_water_equivalent': self.SWE,  # [mm]
                'water_closure': MBE,  # [mm d-1]
                'phenostate': fPheno,  # [-]
                'leaf_area_index': self.LAI,  # [m2 m-2]
                'stomatal_conductance': Gc,  # [m s-1]
                'degree_day_sum': self.DDsum,  # [degC]
                'fLAI': self.LAI,
                'water_storage': self.W,
                'snowfall': Sfall, # [mm d-1]
                'rainfall': Rfall  # [mm d-1]
                }

        return results
    '''
    def update_daily(self, T, doy):
        """
        updates temperature sum, leaf-area development, phenology and
        computes effective parameters for grid-cell
        Args:
            T - daily mean temperature (degC)
            doy - day of year
        Returns:
            None
        """

        self._degreeDays(T, doy)
        self._photoacclim(T)

        # deciduous relative leaf-area index
        self._lai_dynamics(doy)

        # canopy effective photosynthesis-stomatal conductance parameters:
        LAI = 0.0
        Amax = 0.0
        q50 = 0.0
        g1 = 0.0
        for pt in self.ptypes.keys():
            if self.ptypes[pt]['lai_cycle']:
                pt_lai = self.ptypes[pt]['LAImax'] * self._relative_lai
            else:
                pt_lai = self.ptypes[pt]['LAImax']
            LAI += pt_lai
            Amax += pt_lai * self.ptypes[pt]['amax']
            q50 += pt_lai * self.ptypes[pt]['q50']
            g1 += pt_lai * self.ptypes[pt]['g1']

        self.LAI = LAI + eps

        #print(doy, LAI, Amax / self.LAI, g1 / self.LAI)

        self.physpara.update({'Amax': Amax / self.LAI, 'q50': q50 / self.LAI, 'g1': g1 / self.LAI})
        '''

    def _degreeDays(self, T, doy):
        """
        Calculates and updates degree-day sum from the current mean Tair.
        INPUT:
            T - daily mean temperature (degC)
            doy - day of year 1...366 (integer)
        """
        To = 5.0  # threshold temperature
        self.DDsum = self.DDsum + np.maximum(0.0, T - To)

        #reset at beginning of year
        self.DDsum[doy * self.cmask == 1] = 0.

    def _photoacclim(self, T):
        """
        computes new stage of temperature acclimation and phenology modifier.
        Peltoniemi et al. 2015 Bor.Env.Res.
        IN: object, T = daily mean air temperature
        OUT: fPheno - phenology modifier [0...1], updates object state
        """

        self.X = self.X + 1.0 / self.phenopara['tau'] * (T - self.X)  # degC
        S = np.maximum(self.X - self.phenopara['xo'], 0.0)
        fPheno = np.maximum(self.phenopara['fmin'],
                            np.minimum(S / self.phenopara['smax'], 1.0))
        return fPheno

    def _lai_dynamics(self, doy):
        """
        Seasonal cycle of deciduous leaf area

        Args:
            self - object
            doy - day of year

        Returns:
            none, updates state variables self.LAIdecid, self._growth_stage,
            self._senec_stage
        """

        lai_min = self.phenopara['lai_decid_min']
        ddo = self.phenopara['ddo']
        ddur = self.phenopara['ddur']
        sso = self.phenopara['sso']
        sdur = self.phenopara['sdur']

        # growth phase
        self._growth_stage += 1.0 / ddur
        f = np.minimum(1.0, lai_min + (1.0 - lai_min) * self._growth_stage)

        # beginning of year
        ix = np.where(self.DDsum <= ddo)
        f[ix] = lai_min
        self._growth_stage[ix] = 0.
        self._senesc_stage[ix] = 0.

        # senescence phase
        ix = np.where(doy > sso)
        self._growth_stage[ix] = 0.
        self._senesc_stage[ix] += 1.0 / sdur
        f[ix] = 1.0 - (1.0 - lai_min) * np.minimum(1.0, self._senesc_stage[ix])

        # update self.LAIdecid and total LAI
        self._LAIdecid = self._LAIdecid_max * f
        self._LAIgrass = self._LAIgrass_max * f
        self.LAI = self._LAIconif + self._LAIdecid #+ self._LAIshrub + self._LAIgrass
        return f

    def dry_canopy_et(self, D, Qp, AE, Ta, Ra=25.0, Ras=250.0, CO2=380.0, Rew=1.0, beta=1.0, fPheno=1.0):
        """
        Computes ET from 2-layer canopy in absense of intercepted precipitiation,
        i.e. in dry-canopy conditions
        IN:
           self - object
           D - vpd in kPa
           Qp - PAR in Wm-2
           AE - available energy in Wm-2
           Ta - air temperature degC
           Ra - aerodynamic resistance (s/m)
           Ras - soil aerodynamic resistance (s/m)
           CO2 - atm. CO2 mixing ratio (ppm)
           Rew - relative extractable water [-]
           beta - relative soil conductance for evaporation [-]
           fPheno - phenology modifier [-]
        Args:
           Tr - transpiration rate (mm s-1)
           Efloor - forest floor evaporation rate (mm s-1)
           Gc - canopy conductance (integrated stomatal conductance)  (m s-1)
        SOURCES:
        Launiainen et al. (2016). Do the energy fluxes and surface conductance
        of boreal coniferous forests in Europe scale with leaf area?
        Global Change Biol.
        Modified from: Leuning et al. 2008. A Simple surface conductance model
        to estimate regional evaporation using MODIS leaf area index and the
        Penman-Montheith equation. Water. Resources. Res., 44, W10419
        Original idea Kelliher et al. (1995). Maximum conductances for
        evaporation from global vegetation types. Agric. For. Met 85, 135-147

        Samuli Launiainen, Luke
        Last edit: 13.6.2018: TESTING UPSCALING
        """

        # ---Amax and g1 as LAI -weighted average of conifers and decid.

        rhoa = 101300.0 / (8.31 * (Ta + 273.15)) # mol m-3
        Amax = 1./self.LAI * (self._LAIconif * self.physpara['amax']
                + self._LAIdecid *self.physpara['amax']) # umolm-2s-1

        g1 = 1./self.LAI * (self._LAIconif * self.physpara['g1_conif']
                + self._LAIdecid *self.physpara['g1_decid'])

        kp = self.physpara['kp']  # (-) attenuation coefficient for PAR
        q50 = self.physpara['q50']  # Wm-2, half-sat. of leaf light response
        rw = self.physpara['rw']  # rew parameter
        rwmin = self.physpara['rwmin']  # rew parameter

        tau = np.exp(-kp * self.LAI)  # fraction of Qp at ground relative to canopy top

        """--- canopy conductance Gc (integrated stomatal conductance)----- """

        # fQ: Saugier & Katerji, 1991 Agric. For. Met., eq. 4. Leaf light response = Qp / (Qp + q50)
        fQ = 1./ kp * np.log((kp*Qp + q50) / (kp*Qp*np.exp(-kp * self.LAI) + q50 + eps))

        # the next formulation is from Leuning et al., 2008 WRR for daily Gc; they refer to
        # Kelliher et al. 1995 AFM but the resulting equation is not exact integral of K95.
        # fQ = 1./ kp * np.log((Qp + q50) / (Qp*np.exp(-kp*self.LAI) + q50))

        # soil moisture response: Lagergren & Lindroth, xxxx"""
        fRew = np.minimum(1.0, np.maximum(Rew / rw, rwmin))
        #fRew = Rew

        # CO2 -response of canopy conductance, derived from APES-simulations
        # (Launiainen et al. 2016, Global Change Biology). relative to 380 ppm
        fCO2 = 1.0 - 0.387 * np.log(CO2 / 380.0)

        # leaf level light-saturated gs (m/s)
        gs = np.minimum(1.6*(1.0 + g1 / np.sqrt(D))*Amax / 380. / rhoa, 0.1)  # large values if D -> 0

        # canopy conductance
        Gc = gs * fQ * fRew * fCO2 * fPheno
        Gc[np.isnan(Gc)] = eps

        """ --- transpiration rate --- """
        Tr = penman_monteith((1.-tau)*AE, 1e3*D, Ta, Gc, 1./Ra, units='mm')
        Tr[Tr < 0] = 0.0

        """--- forest floor evaporation rate--- """
        # soil conductance is function of relative water availability
        # gcs = 1. / self.soilrp * beta**2.0
        # beta = Wliq / FC; Best et al., 2011 Geosci. Model. Dev. JULES
        Gcs = self.gsoil

        Efloor = beta * penman_monteith(tau * AE, 1e3*D, Ta, Gcs, 1./Ras, units='mm')
        Efloor[self.SWE > 0] = 0.0  # no evaporation from floor if snow on ground or beta == 0

        return Tr, Efloor, Gc


    def canopy_water_snow(self, dt, T, Prec, AE, D, Ra=25.0, U=2.0):
        """
        Calculates canopy water interception and SWE during timestep dt
        Args:
            self - object
            dt - timestep [s]
            T - air temperature (degC)
            Prec - precipitation rate during (mm d-1)
            AE - available energy (~net radiation) (Wm-2)
            D - vapor pressure deficit (kPa)
            Ra - canopy aerodynamic resistance (s m-1)
        Returns:
            self - updated state W, Wf, SWE, SWEi, SWEl
            Infil - potential infiltration to soil profile (mm)
            Evap - evaporation / sublimation from canopy store (mm)
            MBE - mass balance error (mm)
        Samuli Launiainen & Ari Laurén 2014 - 2017
        Last edit 12 / 2017
        """

        # quality of precipitation
        Tmin = 0.0  # 'C, below all is snow
        Tmax = 1.0  # 'C, above all is water
        Tmelt = 0.0  # 'C, T when melting starts

        # storage capacities mm
        Wmax = self.wmax * self.LAI
        Wmaxsnow = self.wmaxsnow * self.LAI

        # melting/freezing coefficients mm/s
        Kmelt = self.Kmelt - 1.64 * self.cf / dt  # Kuusisto E, 'Lumi Suomessa'
        Kfreeze = self.Kfreeze

        kp = self.physpara['kp']
        tau = np.exp(-kp*self.LAI)  # fraction of Rn at ground

        # inputs to arrays, needed for indexing later in the code
        gridshape = np.shape(self.LAI)  # rows, cols

        if np.shape(T) != gridshape:
            T = np.ones(gridshape) * T
            Prec = np.ones(gridshape) * Prec

        # latent heat of vaporization (Lv) and sublimation (Ls) J kg-1
        Lv = 1e3 * (3147.5 - 2.37 * (T + 273.15))
        Ls = Lv + 3.3e5

        # compute 'potential' evaporation / sublimation rates for each grid cell
        Ga = 1. / Ra  # aerodynamic conductance

        # resistance for snow sublimation adopted from:
        # Pomeroy et al. 1998 Hydrol proc; Essery et al. 2003 J. Climate;
        # Best et al. 2011 Geosci. Mod. Dev.
        # ri = (2/3*rhoi*r**2/Dw) / (Ce*Sh*W) == 7.68 / (Ce*Sh*W

        Ce = 0.01*((self.W + eps) / Wmaxsnow)**(-0.4)  # exposure coeff (-)
        Sh = (1.79 + 3.0*U**0.5)  # Sherwood numbner (-)

        gi = np.where(T <= Tmin, Sh*self.W*Ce / 7.68 + eps, 1e6) # m s-1
        Lambda = np.where(T <= Tmin, Ls, Lv)
        # evaporation of interception storage, mm
        erate = np.where(Prec==0,
                         dt / Lambda * penman_monteith((1.0 - tau)*AE, 1e3*D, T, gi, Ga, units='W'),
                         0.0)

        # ---state of precipitation [as water (fW) or as snow(fS)]
        fW = np.where(T >= Tmax, 1.0, 0.0)

        ix = ((T > Tmin) & (T < Tmax))
        fW[ix] = (T[ix] - Tmin) / (Tmax - Tmin)

        fS = 1.0 - fW

        sf = fS * Prec
        rf = fW * Prec

        """ --- initial conditions for calculating mass balance error --"""
        Wo = self.W  # canopy storage
        SWEo = self.SWE  # Snow water equivalent mm

        """ --------- Canopy water storage change -----"""
        # snow unloading from canopy, ensures also that seasonal LAI development does
        # not mess up computations
        Unload = np.where(T >= Tmax, np.maximum(self.W - Wmax, 0.0), 0.0)
        self.W = self.W - Unload

        # Interception of rain or snow: asymptotic approach of saturation.
        # Hedstrom & Pomeroy 1998. Hydrol. Proc 12, 1611-1625;
        # Koivusalo & Kokkonen 2002 J.Hydrol. 262, 145-164.
        # above Tmin, interception capacity equals that of liquid precip
        Interc = np.where(T < Tmin,
                (Wmaxsnow - self.W)* (1.0 - np.exp(-(self.cf / Wmaxsnow) * Prec)),
                np.maximum(0.0, (Wmax - self.W))* (1.0 - np.exp(-(self.cf / Wmax) * Prec)))

        self.W = self.W + Interc  # new canopy storage, mm

        Trfall = Prec + Unload - Interc  # Throughfall to field layer or snowpack

        # evaporate from canopy and update storage
        Evap = np.minimum(erate, self.W)  # mm
        self.W = self.W - Evap

        """ Snowpack (in case no snow, all Trfall routed to floor) """
        # melting positive, freezing negative
        Melt_Freeze = np.where(T >= Tmelt,
                np.minimum(self.SWEi, Kmelt * dt * (T - Tmelt)),
                -np.minimum(self.SWEl, Kfreeze * dt * (Tmelt - T)))

        # amount of water as ice and liquid in snowpack
        Sice = np.maximum(0.0, self.SWEi + fS * Trfall - Melt_Freeze)
        Sliq = np.maximum(0.0, self.SWEl + fW * Trfall + Melt_Freeze)

        PotInf = np.maximum(0.0, Sliq - Sice * self.R)  # mm
        Sliq = np.maximum(0.0, Sliq - PotInf)  # mm, liquid water in snow

        # update Snowpack state variables
        self.SWEl = Sliq
        self.SWEi = Sice
        self.SWE = self.SWEl + self.SWEi

        # mass-balance error mm
        MBE = (self.W + self.SWE) - (Wo + SWEo) - (Prec - Evap - PotInf)

        return PotInf, Trfall, Evap, Interc, MBE, erate, Unload, fS + fW, sf, rf


""" *********** utility functions ******** """

# @staticmethod
def degreeDays(dd0, T, Tbase, doy):
    """
    Calculates degree-day sum from the current mean Tair.
    INPUT:
        dd0 - previous degree-day sum (degC)
        T - daily mean temperature (degC)
        Tbase - base temperature at which accumulation starts (degC)
        doy - day of year 1...366 (integer)
    OUTPUT:
        x - degree-day sum (degC)
   """
    if doy == 1:  # reset in the beginning of the year
        dd0 = 0.
    return dd0 + max(0, T - Tbase)


# @staticmethod
def eq_evap(AE, T, P=101300.0, units='W'):
    """
    Calculates the equilibrium evaporation according to McNaughton & Spriggs,\
    1986.
    INPUT:
        AE - Available energy (Wm-2)
        T - air temperature (degC)
        P - pressure (Pa)
        units - W (Wm-2), mm (mms-1=kg m-2 s-1), mol (mol m-2 s-1)
    OUTPUT:
        equilibrium evaporation rate (Wm-2)
    """
    Mw = 18e-3  # kg mol-1
    # latent heat of vaporization of water [J/kg]
    L = 1e3 * (2500.8 - 2.36 * T + 1.6e-3 * T ** 2 - 6e-5 * T ** 3)
    # latent heat of sublimation [J/kg]
    if T < 0:
        L = 1e3 * (2834.1 - 0.29 * T - 0.004 * T ** 2)

    _, s, g = e_sat(T, P)

    x = np.divide((AE * s), (s + g))  # Wm-2 = Js-1m-2
    if units == 'mm':
        x = x / L  # kg m-2 s-1 = mm s-1
    elif units == 'mol':
        x = x / L / Mw  # mol m-2 s-1
    x = np.maximum(x, 0.0)
    return x


# @staticmethod
def e_sat(T, P=101300, Lambda=2450e3):
    """
    Computes saturation vapor pressure (Pa), slope of vapor pressure curve
    [Pa K-1]  and psychrometric constant [Pa K-1]
    IN:
        T - air temperature (degC)
        P - ambient pressure (Pa)
        Lambda - lat heat of vapor [J/kg]
    OUT:
        esa - saturation vapor pressure in Pa
        s - slope of saturation vapor pressure curve (Pa K-1)
        g - psychrometric constant (Pa K-1)
    """
    cp = 1004.67  # J/kg/K

    esa = 1e3 * 0.6112 * np.exp((17.67 * T) / (T + 273.16 - 29.66))  # Pa

    s = 17.502 * 240.97 * esa / ((240.97 + T) ** 2)
    g = P * cp / (0.622 * Lambda)
    return esa, s, g

# @staticmethod
def penman_monteith(AE, D, T, Gs, Ga, P=101300.0, units='W'):
    """
    Computes latent heat flux LE (Wm-2) i.e evapotranspiration rate ET (mm/s)
    from Penman-Monteith equation
    INPUT:
       AE - available energy [Wm-2]
       VPD - vapor pressure deficit [Pa]
       T - ambient air temperature [degC]
       Gs - surface conductance [ms-1]
       Ga - aerodynamic conductance [ms-1]
       P - ambient pressure [Pa]
       units - W (Wm-2), mm (mms-1=kg m-2 s-1), mol (mol m-2 s-1)
    OUTPUT:
       x - evaporation rate in 'units'
    """
    # --- constants
    cp = 1004.67  # J kg-1 K-1
    rho = 1.25  # kg m-3
    Mw = 18e-3  # kg mol-1
    L = 1e3 * (3147.5 - 2.37 * (T + 273.15))
    _, s, g = e_sat(T, P, L)  # slope of sat. vapor pressure, psycrom const

    x = (s * AE + rho * cp * Ga * D) / (s + g * (1.0 + Ga / (Gs + eps)))  # Wm-2

    if units == 'mm':
        x = x / L  # kgm-2s-1 = mms-1
    if units == 'mol':
        x = x / L / Mw  # mol m-2 s-1

    x = np.maximum(x, 0.0)
    return x


# @staticmethod
# def aerodynamic_conductance_from_ust(Ust, U, Stanton):
#    """
#    computes canopy aerodynamic conductance (ms-1) from frict. velocity
#    IN:
#       Ustar - friction velocity (ms-1)
#       U - mean wind speed at flux measurement heigth (ms-1)
#       Stanton - Stanton number (kB-1) for quasi-laminar boundary layer
#           resistance. Typically kB=1...12, use 2 for vegetation ecosystems
#           (Verma, 1989, Garratt and Hicks, 1973)
#    OUT:
#       Ga - aerodynamic conductance [ms-1]
#    """
#    kv = 0.4  # von Karman constant
#    ra = U / (Ust ** 2.0 + eps) + Stanton / (kv * (Ust + eps))  # sm-1
#    Ga = 1.0 / ra  # ms-1
#    return Ga

def aerodynamics(LAI, hc, Uo, w=0.01, zm=2.0, zg=0.5, zos=0.01):
    """
    computes wind speed at ground and canopy + boundary layer conductances
    Computes wind speed at ground height assuming logarithmic profile above and
    exponential within canopy
    Args:
        LAI - one-sided leaf-area /plant area index (m2m-2)
        hc - canopy height (m)
        Uo - mean wind speed at height zm (ms-1)
        w - leaf length scale (m)
        zm - wind speed measurement height above canopy (m)
        zg - height above ground where Ug is computed (m)
        zos - forest floor roughness length, ~ 0.1*roughness element height (m)
    Returns:
        ra - canopy aerodynamic resistance (sm-1)
        rb - canopy boundary layer resistance (sm-1)
        ras - forest floor aerod. resistance (sm-1)
        ustar - friction velocity (ms-1)
        Uh - wind speed at hc (ms-1)
        Ug - wind speed at zg (ms-1)
    SOURCE:
       Cammalleri et al. 2010 Hydrol. Earth Syst. Sci
       Massman 1987, BLM 40, 179 - 197.
       Magnani et al. 1998 Plant Cell Env.
    """
    zm = hc + zm  # m
    kv = 0.4  # von Karman constant (-)
    beta = 285.0  # s/m, from Campbell & Norman eq. (7.33) x 42.0 molm-3
    alpha = LAI / 2.0  # wind attenuation coeff (Yi, 2008 eq. 23)
    d = 0.66*hc  # m
    zom = 0.123*hc  # m
    zov = 0.1*zom
    zosv = 0.1*zos

    # solve ustar and U(hc) from log-profile above canopy
    ustar = Uo * kv / np.log((zm - d) / zom)
    Uh = ustar / kv * np.log((hc - d) / zom)

    # U(zg) from exponential wind profile
    zn = np.minimum(zg / hc, 1.0)  # zground can't be above canopy top
    Ug = Uh * np.exp(alpha*(zn - 1.0))

    # canopy aerodynamic & boundary-layer resistances (sm-1). Magnani et al. 1998 PCE eq. B1 & B5
    #ra = 1. / (kv*ustar) * np.log((zm - d) / zom)
    ra = 1./(kv**2.0 * Uo) * np.log((zm - d) / zom) * np.log((zm - d) / zov)
    rb = 1. / LAI * beta * ((w / Uh)*(alpha / (1.0 - np.exp(-alpha / 2.0))))**0.5

    # soil aerodynamic resistance (sm-1)
    ras = 1. / (kv**2.0*Ug) * (np.log(zg / zos))*np.log(zg / (zosv))

    #print('ra', ra, 'rb', rb)
    ra = ra + rb
    return ra, rb, ras, ustar, Uh, Ug

def wind_profile(LAI, hc, Uo, z, zm=2.0, zg=0.2):
    """
    Computes wind speed at ground height assuming logarithmic profile above and
    hyperbolic cosine profile within canopy
    INPUT:
        LAI - one-sided leaf-area /plant area index (m2m-2)
        hc - canopy height (m)
        Uo - mean wind speed at height zm (ms-1)
        zm - wind speed measurement height above canopy (m)
        zg - height above ground where U is computed
    OUTPUT:
        Uh - wind speed at hc (ms-1)
        Ug - wind speed at zg (ms-1)
    SOURCE:
       Cammalleri et al. 2010 Hydrol. Earth Syst. Sci
       Massman 1987, BLM 40, 179 - 197.
    """

    k = 0.4  # von Karman const
    Cd = 0.2  # drag coeff
    alpha = 1.5  # (-)

    zm = zm + hc
    d = 0.66*hc
    zom = 0.123*hc
    beta = 4.0 * Cd * LAI / (k**2.0*alpha**2.0)
    # solve ustar and U(hc) from log-profile above canopy
    ustar = Uo * k / np.log((zm - d) / zom)  # m/s

    U = np.ones(len(z))*np.NaN

    # above canopy top wind profile is logarithmic
    U[z >= hc] = ustar / k * np.log((z[z >= hc] - d) / zom)

    # at canopy top, match log and exponential profiles
    Uh = ustar / k * np.log((hc - d) / zom)  # m/s

    # within canopy hyperbolic cosine profile
    U[z <= hc] = Uh * (np.cosh(beta * z[z <= hc] / hc) / np.cosh(beta))**0.5

    return U, ustar, Uh

def daylength(LAT, DOY):
    """
    Computes daylength from location and day of year.

    Args:
        LAT - in deg, float or arrays of floats
        doy - day of year, float or arrays of floats

    Returns:
        dl - daylength (hours), float or arrays of floats
    """
    CF = np.pi / 180.0  # conversion deg -->rad

    LAT = LAT*CF
    # ---> compute declination angle
    xx = 278.97 + 0.9856*DOY + 1.9165*np.sin((356.6 + 0.9856*DOY)*CF)
    DECL = np.arcsin(0.39785*np.sin(xx*CF))
    del xx

    # --- compute day length, the period when sun is above horizon
    # i.e. neglects civil twilight conditions
    cosZEN = 0.0

    dl = 2.0*np.arccos(cosZEN - np.sin(LAT)*np.sin(DECL) / (np.cos(LAT)*np.cos(DECL))) / CF / 15.0  # hours

    return dl

def read_ini(inifile):
    """read_ini(inifile): reads canopygrid.ini parameter file into pp dict"""

    cfg = configparser.ConfigParser()
    cfg.read(inifile)

    pp = {}
    for s in cfg.sections():
        section = s.encode('ascii', 'ignore')
        pp[section] = {}
        for k, v in cfg.items(section):
            key = k.encode('ascii', 'ignore')
            val = v.encode('ascii', 'ignore')
            if section == 'General':  # 'general' section
                pp[section][key] = val
            else:
                pp[section][key] = float(val)

    pp['General']['dt'] = float(pp['General']['dt'])

    pgen = pp['General']
    cpara = pp['CanopyGrid']
    return pgen, cpara
