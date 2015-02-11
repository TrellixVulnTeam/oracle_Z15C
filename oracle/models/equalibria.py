#!/usr/bin/env python
# -*- coding: utf-8 -*-

""" Equialibria (excitation & ionisation balance) model for stellar spectra. """

from __future__ import absolute_import, print_function

__author__ = "Andy Casey <arc@ast.cam.ac.uk>"

import logging
import numpy as np
from scipy import stats, ndimage, optimize as op
from astropy import (modeling, table, units as u)

from oracle import atmospheres, specutils, synthesis, utils
from oracle.models.model import Model
from oracle.models import transitions
from oracle.models.jacobian \
    import approximate as stellar_parameter_jacobian_approximation

logger = logging.getLogger("oracle")

import matplotlib.pyplot as plt



class EqualibriaModel(Model):

    """
    This class performs excitation and ionization balances in order to 
    provide a point estimate of the stellar parameters.
    """

    # Default configuration for a EqualibriaModel class
    _default_config = {
        "model": {
            "redshift": True,
            "instrumental_resolution": True,
            "continuum": False,
            "profile_function": "gaussian"
        },
        "settings": {
            "threads": 1
        }
    }

    def __init__(self, configuration, **kwargs):
        """
        Initialise an Equialibria Model class.
        
        :param configuration:
            The path to the configuration file that contains settings for the
            class, a dictionary describing the configuration, or a string 
            containing the configuration in a YAML-dump format. Only YAML-style
            formats are accepted.

        :type configuration:
            str or dict
        """

        super(EqualibriaModel, self).__init__(configuration)

        # Initialise the atomic transitions.
        self.atomic_transitions = self._initialise_atomic_transitions()

        # Initiate an atmosphere interpolator.
        atmosphere_kwds = self.config["model"].get("atmosphere_kwargs", {})
        self._interpolator = atmospheres.interpolator(**atmosphere_kwds)
        return None


    # For pickling and unpickling the class.
    def __getstate__(self):
        allowed_keys = ("config", "atomic_transitions", "_initial_theta")
        state = self.__dict__.copy()
        for key in state.keys():
            if key not in allowed_keys:
                del state[key]
        return state

    def __setstate__(self, state):
        self.__dict__ = state.copy()


    def _initialise_atomic_transitions(self):
        """
        Load the atomic transition information from the configuration.
        """

        # Load atomic transitions.
        filename = self.config["model"].get("atomic_transitions_filename", None)
        if filename is not None:
            format = self.config["model"].get("atomic_transitions_format", None)
            try:
                atomic_transitions = \
                    table.Table.read(filename, format=format)
            except:
                raise ValueError("failed to load atomic transitions filename "
                    "from {} -- try specifying `atomic_transitions_format` in "
                    "the model configuration".format(filename))

            # Delete the reference to the filename in the config, because if we
            # don't then we will run into pickling problems.
            del self.config["model"]["atomic_transitions_filename"]

        else:
            # Must be giving them individually.
            transitions = self.config["model"].get("atomic_transitions", None)
            if transitions is None:
                raise ValueError("no atomic transitions found")
            atomic_transitions = table.Table(data=transitions)

            # Delete the reference to the transitions in the config, because if
            # we don't then we will run into pickling problems later.
            del self.config["model"]["atomic_transitions"]

        # Verify that we have the minimum columns required.
        for column in ("wavelength", "species", "excitation_potential", "loggf"):
            if column not in atomic_transitions.dtype.names:
                raise ValueError("missing requried column '{}' in the atomic "
                    "transitions table".format(column))

        if "clean" not in atomic_transitions.dtype.names:
            logger.warn("No 'clean' column found in atomic transitions. All the"
                "transitions are assumed to have no blending by nearby lines.")

        # Create a default table with the additional columns we require.
        N = len(atomic_transitions)
        columns = [
            table.Column(np.ones(N) * np.nan,
                name="equivalent_width", unit="milliAngstrom"),
            table.Column(np.ones(N, dtype=bool),
                name="clean", dtype=bool),
            table.Column(np.ones(N) * np.nan,
                name="abundance"),
            table.Column([""] * N,
                name="custom_mask", dtype="|S32")
            ]
        for column in columns:
            if column.name not in atomic_transitions.columns:
                atomic_transitions.add_column(column)
            else:
                index = atomic_transitions.colnames.index(column.name)
                old_dtype = atomic_transitions.columns[index].dtype
                if column.dtype != old_dtype:
                    new_column = table.Column(
                        data=atomic_transitions[column.name],
                        name=column.name, dtype=column.dtype)

                    atomic_transitions.remove_column(column.name)
                    atomic_transitions.add_column(new_column)

        # Add some units.
        atomic_transitions["wavelength"].unit = u.Angstrom
        atomic_transitions["excitation_potential"].unit = u.eV
        atomic_transitions["equivalent_width"].unit = "milliAngstrom"

        # Check for custom masks.
        for mask in set(atomic_transitions["custom_mask"]).difference({""}):
            if mask not in self.config["model"].get("custom_mask", {}):
                raise ValueError("cannot find custom mask '{}' in the model "
                    "configuration".format(mask))

        return atomic_transitions



    def fit_atomic_transitions(self, data, effective_temperature=None,
        surface_gravity=None, metallicity=None, microturbulence=None,
        wavelength_region=2.5, outlier_modeling=True, max_outlier_profiles=5,
        **kwargs):
        """
        Fit absorption profiles to the atomic transitions in this model and
        account for the nearby blends.

        :param data:
            The observed spectra.

        :type data:
            list of :class:`oracle.specutils.Spectrum1D`

        :param effective_temperature: [sometimes optional]
            The effective temperature for the photosphere.

            When there are nearby blending transitions, these lines need to be 
            synthesised in order to accurately measure the actual transition we
            care about. Thus, if any blending (non-clean) transitions are within
            `wavelength_region` of a clean line then this parameter is required.

        :type effective_temperature:
            float

        :param surface_gravity: [sometimes optional]
            The surface gravity for the photosphere.

            When there are nearby blending transitions, these lines need to be 
            synthesised in order to accurately measure the actual transition we
            care about. Thus, if any blending (non-clean) transitions are within
            `wavelength_region` of a clean line then this parameter is required.

        :type surface_gravity:
            float

        :param metallicity: [sometimes optional]
            The scaled-solar metallicity for the photosphere.

            When there are nearby blending transitions, these lines need to be 
            synthesised in order to accurately measure the actual transition we
            care about. Thus, if any blending (non-clean) transitions are within
            `wavelength_region` of a clean line then this parameter is required.

        :type metallicity:
            float

        :param microturbulence: [sometimes optional]
            The microturbulence for the photosphere. This is not required for
            <3D> models.

            When there are nearby blending transitions, these lines need to be 
            synthesised in order to accurately measure the actual transition we
            care about. Thus, if any blending (non-clean) transitions are within
            `wavelength_region` of a clean line then this parameter is required.

        :type microturbulence:
            float

        :param wavelength_region: [optional]
            The +/- region (in Angstroms) around each atomic transition to
            consider.

        :type wavelength_region:
            float

        :param outlier_modeling: [optional]
            Detect inaccurate fits due to blending lines and account for them.
            If enabled, then a poor fit is detected when the centroid of the 
            atomic transition is not within 5 per cent of the data, or if the
            profile FWHM has hit an upper boundary. When this happens the code
            will detect regions that are most discrepant, and attempt to fit
            them with additional profiles (using the same profile FWHM).

        :type outlier_modeling:
            bool

        :param max_outlier_profiles: [optional]
            The maximum number of outlier profiles to add for each atomic
            transition. This keyword argument is ignored if `outlier_modeling`
            is set to `False`.
        """

        oversampling_rate = int(kwargs.pop("oversampling_rate", 4))
        if 1 > oversampling_rate:
            raise ValueError("oversampling rate must be a positive integer")

        # TODO this should really be changed to a limit based on radial velocity.
        wavelength_tolerance = abs(kwargs.pop("wavelength_tolerance", 0))

        # Allow the user to specify bounds on the data.
        # (And here we will set some sensible ones.)
        common_bounds = kwargs.pop("bounds", {
            "stddev": (0, 0.3),
            "amplitude": (0, 1)
        })
        if "wavelength" in common_bounds or "mean" in common_bounds:
            raise ValueError("apply bounds on the profile location through the "
                "wavelength_tolerance keyword argument")

        # Create a handy function to update the compound model properties.
        def _update_compound_model(compound_model, wavelength):

            # Deal with the line we care about first.
            single_model = hasattr(compound_model, "mean")
            transition_mean_key = ["mean_0", "mean"][single_model]
            if wavelength_tolerance > 0:
                compound_model.bounds[transition_mean_key] = (
                    wavelength - wavelength_tolerance,
                    wavelength + wavelength_tolerance
                )
            else:
                compound_model.fixed[transition_mean_key] = True

            for param_name in compound_model.param_names:
                if param_name.startswith("mean_") \
                and param_name != transition_mean_key:
                    # It's an outlier line. Fix the wavelength.
                    compound_model.fixed[param_name] = True

                # Apply common bounds.
                try:
                    prefix, num = param_name.split("_")

                except ValueError:
                    prefix, num = param_name, "0"

                if prefix in common_bounds.keys():
                    compound_model.bounds[param_name] = common_bounds[prefix]

                # Tie the stddevs to the original absorption profile.
                # The stddevs between the absorption transition we care
                # about and the outlier transition are related by:
                # R = lambda_1/delta_lambda_1 = lambda_2/delta_labmda_2
                if prefix == "stddev" and num != "0":
                    compound_model.tied[param_name] = lambda _: _.stddev_0 * \
                        getattr(compound_model, "mean_{}".format(num))/_.mean_0
            return True


        photosphere = None
        # Interpolate a photosphere if we have the information to do so.
        if None not in (effective_temperature, surface_gravity, metallicity):
            photosphere = self._interpolator(
                effective_temperature, surface_gravity, metallicity)

        # Identify clean transitions that are actually in the data.
        # If data_indices is -1 it means the line was not found in the data
        data_indices = \
            wavelengths_in_data(self.atomic_transitions["wavelength"], data)
        measurable = self.atomic_transitions["clean"] * (data_indices > -1)

        # Create the subset containing the measurable lines.
        transition_indices = np.where(measurable)[0]
        transitions = self.atomic_transitions[measurable]
        data_indices = data_indices[measurable]

        stddevs = []
        fitted_profiles = []
        for transition_index, transition, data_index \
        in zip(transition_indices, transitions, data_indices):
        
            wavelength = transition["wavelength"]

            # Look for nearby transitions within the wavelength region and
            # ignore this line.
            blending = wavelength_region >= \
                np.abs(self.atomic_transitions["wavelength"] - wavelength) 
            blending[transition_index] = False

            # Slice the data +/- some region.
            spectrum = data[data_index]
            disp_indices = spectrum.disp.searchsorted([
                wavelength - wavelength_region,
                wavelength + wavelength_region
                ]) + [0, 1]
            x = spectrum.disp.__getslice__(*disp_indices)
            y = spectrum.flux.__getslice__(*disp_indices)
            
            # Calculate an initial stddev value based on the x spacing.
            initial_stddev = 5 * np.diff(x).mean()
            
            # Apply any custom mask.
            # TODO This should just remove the masked pixels from x and y,
            #      because setting them to NaN will break the fitter.

            
            # TODO the amplitude initial guess will have to be udpated in the 
            #      presence of continuum.
            _ = x.searchsorted(wavelength)
            initial_amplitude = 1.0 - y[_]

            # Any continuum?
            continuum_order = self._continuum_order(data_index)
            if continuum_order > -1:
                # TODO I specify order and astropy uses degree. Switch to degree!
                #profile_init *= modeling.models.Polynomial1D(continuum_order + 1)

                # Set initial estimates of continuum.
                # TODO

                # This will probably fuck up the parameter names.
                raise NotImplementedError

            # Any synthesis?
            if np.any(blending):
                if photosphere is None:
                    raise ValueError("transition at {0:.3f} has blending lines "
                        "within {1:.0f} (so a synthesis approach is needed) but"
                        " not all stellar parameters were given".format(
                            wavelength, wavelength_region))

                # Synthesise a spectrum (with oversampling)
                synth_pixel_size = np.diff(x).mean()/oversampling_rate
                synth_disp, synth_flux = synthesis.moog.synthesise(
                    self.atomic_transitions[blending], photosphere,
                    microturbulence=microturbulence,
                    wavelength_region=[x.min(), x.max()],
                    wavelength_step=synth_pixel_size)

                # Create a custom class that uses the synthesised spectrum.
                class GaussianAbsorption1D(modeling.Fittable1DModel):

                    amplitude = modeling.Parameter(default=1)
                    mean = modeling.Parameter(default=0)
                    stddev = modeling.Parameter(default=1)
                    # TODO see issue 18 on GitHub

                    @staticmethod
                    def evaluate(x, amplitude, mean, stddev):
                        convolved = ndimage.gaussian_filter1d(
                            synth_flux, stddev/synth_pixel_size)
                        sampled = np.interp(x, synth_disp, convolved, 1, 1)
                        return sampled * (1.0 - \
                            modeling.models.Gaussian1D.evaluate(x, amplitude,
                                mean, stddev))

                profile = GaussianAbsorption1D(
                    mean=wavelength, amplitude=initial_amplitude,
                    stddev=initial_stddev)

            else:
                profile = modeling.models.GaussianAbsorption1D(
                    mean=wavelength, amplitude=initial_amplitude,
                    stddev=initial_stddev)

            # Update the bounds, fixed, and tied properties.
            _update_compound_model(profile, wavelength)

            fitter = modeling.fitting.LevMarLSQFitter()
            j, y_initial = 1, profile(x)

            while True:

                # Fit the profile.
                profile = fitter(profile, x, y)
                
                # Break here if we have no more outlier modeling to do.
                if not outlier_modeling or max_outlier_profiles == j: break

                # Limitingly-high stddev values are good indicators of nearby
                # lines that have not been accounted for.

                # Having a large % difference between the profile and the data
                # at the transition point is another good indicator of nearby
                # lines that have not been accounted for.
                if (j == 1 and profile.stddev == profile.bounds["stddev"][1])   \
                or (j > 1 and profile.stddev_0 == profile.bounds["stddev_0"][1])\
                or not (1.05 > y[_]/profile(x[_]) > 0.95): #absorption is 5% off

                    # Add a(nother) outlier absorption profile to this model at
                    # the location where a blending line is most likely to be.

                    # But ignore locations near existing lines so we don't just
                    # pile up absorption profiles in the same place.
                    existing_means = [getattr(profile, key) \
                        for key in profile.param_names if key[:4] == "mean"]

                    difference = profile(x) - y
                    for mean in existing_means:
                        __ = np.clip(x.searchsorted([
                            mean - 3 * initial_stddev,
                            mean + 3 * initial_stddev
                        ]) + [0, 1], 0, len(difference) - 1)
                        difference.__setslice__(__[0], __[1], 0)

                    most_discrepant = difference.argmax()

                    # TODO continuum will fuck this up too.
                    profile *= modeling.models.GaussianAbsorption1D(
                        mean=x[most_discrepant], stddev=initial_stddev,
                        amplitude=1.0 - y[most_discrepant])

                    # Update the bounds, fixed, and tied properties.
                    _update_compound_model(profile, wavelength)
                    j += 1

                else:
                    # No outlier treatment required, apparently.
                    break

            # TODO Pickle the model somehow so that we can show it in GUIS later

            # Save the final model fit.
            fitted_profiles.append((x, y, y_initial, profile(x)))

            # Update the equivalent width
            amplitude, stddev = "amplitude", "stddev"
            if not "mean" in profile.param_names:
                stddev += "_0"
                amplitude += "_0"

            stddevs.append(getattr(profile, stddev).value)

            # Integral of Gaussian = amplitude * sigma * sqrt(pi)
            # But the way astropy modeling works, they take 2s = s, so then
            # (2s)**2 -> 4s**2, so we need a 1/4 factor.
            self.atomic_transitions["equivalent_width"][transition_index] = \
                10e3 * np.sqrt(np.pi) * getattr(profile, amplitude) \
                    * 0.25 * getattr(profile, stddev)

        # At this point should we consider re-fitting lines that are deviant
        # from the wavelength vs stddev plot

        """
        for i, (x, y, y_initial, y_fitted) in enumerate(fitted_profiles):

            fig, ax = plt.subplots()
            ax.plot(x,y,c='k')
            ax.plot(x, y_initial, "r:")
            ax.plot(x, y_fitted, 'r')
            is_it_the_first_one = True
            for param_name in profile.param_names:
                if param_name[:4] == "mean":
                    c = "rk"[is_it_the_first_one]
                    ax.axvline(getattr(profile, param_name), c=c)
                    is_it_the_first_one = False


            # Put something about some statistic on the title.
            chi_sq = ((profile(x) - y)**2).sum()
            ax.set_title("chi_sq = {0:.1e}".format(chi_sq))
        """

        # Only update those with good quality constraints.
        stddevs = np.array(stddevs).flatten()
        
        fig, ax = plt.subplots()
        transitions = self.atomic_transitions[transition_indices]
        ok = np.isfinite(transitions["equivalent_width"]) * \
            (transitions["equivalent_width"] > 0) * (stddevs < 0.2)

        ax.scatter(transitions["wavelength"], stddevs)
        
        abundances = synthesis.moog.atomic_abundances(
            transitions, photosphere, microturbulence=microturbulence)
        fig, ax = plt.subplots()
        ax.scatter(transitions["excitation_potential"][ok],
            abundances[ok], facecolor='k')
        raise a


    def estimate_stellar_parameters(self, data, initial_theta=None,
        fitting_frequency=0, **kwargs):
        """
        Return point estimates for the stellar parameters (effective temperature,
        surface gravity, metallicity, microturbulence) using an excitation and
        ionisation balance approach.

        :param data:
            The observed data.

        :type data:
            list of `oracle.specutils.Spectrum1D` objects

        :param initial_theta: [optional]
            Initial estimates of the stellar parameters, radial velocities, and
            continuum parameters (where appropriate).

        :type initial_theta:
            dict

        :param fitting_frequency: [optional]
            This parameter specifies how frequently the profiles be re-fit
            and/or synthesised. If set to zero (default), then fitting is only
            performed once with the initial theta parameters.

        :type fitting_frequency:
            int

        :returns:
            Point estimates of the effective temperature, surface gravity,
            metallicity, and microturbulence.
        """

        if initial_theta is None:
            initial_theta = self.initial_theta(data)

        else:
            logger.warn("assert that all parameters are provided by initial theta")

        if fitting_frequency != 0:
            raise NotImplementedError("sorry")
            # [TODO]

        # Get the initial stellar parameters
        initial_stellar_parameters = [initial_theta[label] for label in \
            ("effective_temperature", "surface_gravity", "[M/H]", 
                "microturbulence")]

        kwds = kwargs.copy()
        kwds.update({
            "full_output": False,
            "initial_theta": initial_theta
        })

        # Get the initial state
        initial_state, transitions_table, atomic_transitions = \
            self.equalibrium_state(data, *initial_stellar_parameters,
                initial_theta=initial_theta, full_output=True)

        # Get usable_transitions
        use = transitions_table["is_filtered"].astype(bool) \
            * ~transitions_table["is_outlier"].astype(bool)
        neutral = (transitions_table["species"][use] % 1) == 0
        ionised = (transitions_table["species"][use] % 1) > 0
        reduced_equivalent_width = np.log(transitions_table["equivalent_width"]\
            /transitions_table["wavelength"])
        #transitions = transitions_table[use].view(float).reshape(use.sum(), -1)[:, :7]

        def state_function(x):
            print("STATE IN", x)

            # Calculate abundances given the stellar parameters
            try:
                abundances = synthesis.atomic_abundances(transitions_table[use],
                    x[:3], microturbulence=x[3])

            except ValueError:
                return np.array([np.nan, np.nan, np.nan, np.nan])

            # Calculate the state
            excitation_slope = stats.linregress(
                x=transitions_table["excitation_potential"][use][neutral],
                y=abundances[neutral])

            # Calculate the ionisation state
            ionisation_state = abundances[neutral].mean() \
                - abundances[ionised].mean()

            # Calculate the abundance state
            metallicity = x[2]
            #metallicity = mh
            abundance_state = (abundances \
                - (atmospheres.solar_abundance(transitions_table["species"][use])
                    + metallicity)).mean()

            # Slope with reduced equivalent width and line abundance
            line_strength_slope = stats.linregress(
                x=reduced_equivalent_width[use][neutral], y=abundances[neutral])

            # Calculate the state
            #state = np.array([
            #    excitation_slope[0],
            #    ionisation_state,
            #    abundance_state,
            #    line_strength_slope[0]
            #])


            # Re-arrange the state
            #teff, vt, logg, feh = stellar_parameters[:4]
            state = np.array([
                excitation_slope[0],
                ionisation_state,
                0.1 * abundance_state,
                line_strength_slope[0],
            ])
            
            if np.any(~np.isfinite(state)):
                raise WTFError()
            print("STATE OUT", x, state, (state**2).sum())
            return state

        # Re-arrange the initial stellar parameters
        initial_stellar_parameters = [initial_theta[label] for label in \
            ("effective_temperature",
                 "surface_gravity", "[M/H]","microturbulence", )]

        """
        # This is for a fitting_frequency == 1
        state_function = lambda x: self.equalibrium_state(data, *x, **kwds)

        # Optimise the state function
        result = op.fsolve(state_function, initial_stellar_parameters,
            fprime=stellar_parameter_jacobian_approximation, col_deriv=True,
            epsfcn=0, xtol=xtol, maxfev=maxfev, full_output=True)
        """

        # Optimisation
        xtol = kwargs.pop("xtol", 1e-10)
        maxfev = kwargs.pop("maxfev", 100)

        result = op.fsolve(state_function, initial_stellar_parameters,
            fprime=stellar_parameter_jacobian_approximation, col_deriv=1,
            epsfcn=0, xtol=xtol, maxfev=maxfev, full_output=True)

        raise a


        raise NotImplementedError

    def equalibrium_state(self, data, effective_temperature, surface_gravity,
        metallicity, microturbulence=None, initial_theta=None, full_output=False,
        **kwargs):
        """
        Return the equilibrium state information for a given set of stellar
        parameters. The equilibrium state information includes the:

        (1) slope of line abundance with the excitation potential
        (2) the mean difference between neutral and ionised lines
        (4) the difference of the input metallicty and mean output metallicity
        (3) slope of line abundance with reduced equivalent width
        
        :param data:
            The observed spectra.

        :type data:
            list of :class:`oracle.specutils.Spectrum1D` objects

        :param effective_temperature:
            The effective temperature to calculate the equilibrium state.

        :type effective_temperature:
            float

        :param surface_gravity:
            The surface gravity to calculate the equilibrium state.

        :type surface_gravity:
            float

        :param metallicity:
            The overall scaled-solar metallicity to calculate the equilibrium
            state.

        :type metallicity:
            float

        :param microturbulence: [sometimes optional]
            The photospheric microturbulence to calculate the equilibrium state.
            This parameter is not required for <3D> models.

        :type microturbulence:
            float

        :param full_output: [optional]
            Return all optional parameters.

        :type full_output:
            bool
        """


        if initial_theta is None:
            initial_theta = {}

        # Interpolate the photospheric quantities
        photospheric_structure = interpolator(effective_temperature,
            surface_gravity, metallicity)    

        # We want to save the profile information and the details of the
        # measured atomic transitions
        profile_initial_theta = kwargs.pop("profile_initial_theta", {})
        profile_initial_theta.update({
            "effective_temperature": effective_temperature,
            "surface_gravity": surface_gravity,
            "metallicity": metallicity,
            "microturbulence": microturbulence,
        })

        # For each atomic transition:
        # (1) Find out which channel it is in.
        # (2) Find out what the continuum order is in that channel.
        # (3) Synthesise some background spectrum around it if necessary.

        # Initialise all the atomic transitions

        for atomic_transition in atomic_transitions:

            # Find which channel the wavelength is in
            ci = minimum_pixel_sampling(data, atomic_transition.wavelength)[1][0]

            # Get the continuum order and limit the continuum order in this
            # region to 2, otherwise we will be overfitting
            co = self._continuum_order(ci)
            co = co if co >= 0 else None
            _profile_initial_theta = profile_initial_theta.copy()
            if co > -1:

                for i in range(co + 1):
                    k = "continuum.{0}.{1}".format(ci, i)
                    if k in initial_theta:
                        _profile_initial_theta["continuum.{}".format(i)] = initial_theta[k]

            fit_profile_kwargs = atomic_transition._init_kwargs.get(
                "_fit_profile_kwargs", {})
            fit_profile_kwargs.update({"full_output": True})
            fit_profile_kwargs.setdefault("continuum_order", co)

            try:
                result = atomic_transition.fit_profile(data[ci],
                    initial_theta=_profile_initial_theta, **fit_profile_kwargs)

            except ValueError:
                continue

            # Save the information
            #(optimal_theta, oc, orc, omf, op_info)

        # Filter out unacceptable measurements
        filtered = kwargs.pop("filter_transition",
            lambda x: (200 > x.equivalent_width > 5))

        is_filtered = np.zeros(len(atomic_transitions), dtype=bool)
        atomic_transitions_arr = np.zeros((len(atomic_transitions), 7))
        for i, atomic_transition in enumerate(atomic_transitions):
            is_filtered[i] = filtered(atomic_transition)
            atomic_transitions_arr[i, :] = [
                atomic_transition.wavelength,
                atomic_transition.species,
                atomic_transition.excitation_potential,
                atomic_transition.loggf,
                atomic_transition.van_der_waals_broadening,
                0,
                atomic_transition.equivalent_width
            ]

        fuck = np.core.records.fromarrays(atomic_transitions_arr.T,
            names=("wavelength", "species", "excitation_potential", "loggf",
                "van_der_waals_broadening", "damp2", "equivalent_width"))


        returned_abundances = synthesis.atomic_abundances(fuck[is_filtered],
            [effective_temperature, surface_gravity, metallicity],
            microturbulence=microturbulence)
        

        # Create a filler array so that filtered transitions will have an
        # abundance of 'nan'
        all_abundances = np.array([np.nan] * len(atomic_transitions_arr))
        all_abundances[is_filtered] = returned_abundances

        is_outlier = np.zeros(len(all_abundances), dtype=bool) # Assume none
        atomic_transitions_rec = np.core.records.fromarrays(
            np.vstack([np.array(atomic_transitions_arr).T,
                all_abundances, is_filtered, is_outlier]),
            names=(
                "wavelength", "species", "excitation_potential", "loggf",
                "van_der_waals_broadening", "damp2", "equivalent_width",
                "abundance", "is_filtered", "is_outlier"))

        # Calculate the excitation and ionisation state
        logger.warn("Assuming only one kind of atomic species")
        neutral_lines = (atomic_transitions_rec["species"] % 1) == 0
        ionised_lines = ~neutral_lines

        outliers_removed = False
        while True:

            # Apply filters
            use_lines = atomic_transitions_rec["is_filtered"].astype(bool) * \
                ~atomic_transitions_rec["is_outlier"].astype(bool)
            neutral_lines *= use_lines
            ionised_lines *= use_lines

            # Excitation slope 
            excitation_slope = stats.linregress(
                x=atomic_transitions_rec["excitation_potential"][neutral_lines],
                y=atomic_transitions_rec["abundance"][neutral_lines])

            # Calculate the ionisation state
            ionisation_state = \
                atomic_transitions_rec["abundance"][neutral_lines].mean() \
                    - atomic_transitions_rec["abundance"][ionised_lines].mean()

            # Calculate the abundance state
            abundance_state = (atomic_transitions_rec["abundance"][use_lines] \
                - (atmospheres.solar_abundance(atomic_transitions_rec["species"][use_lines])
                    + metallicity)).mean()

            # Slope with reduced equivalent width and line abundance
            reduced_equivalent_width = np.log(
                atomic_transitions_rec["equivalent_width"]\
                    /atomic_transitions_rec["wavelength"])
            line_strength_slope = stats.linregress(
                x=reduced_equivalent_width[neutral_lines],
                y=atomic_transitions_rec["abundance"][neutral_lines])

            # Calculate the state
            state = np.array([
                excitation_slope[0],
                ionisation_state,
                abundance_state,
                line_strength_slope[0]
            ])

            if outliers_removed:
                final_state = (excitation_slope, ionisation_state,
                    abundance_state, line_strength_slope)
                break

            else:
                # Remove the outliers?
                initial_state = (excitation_slope, ionisation_state,
                    abundance_state, line_strength_slope)

                outlier_limit = kwargs.pop("outlier_sigma_clip", 3)
                if outlier_limit is None or not np.isfinite(outlier_limit) \
                or 0 >= outlier_limit:
                    # Don't remove any outliers
                    outliers_removed, final_state = False, initial_state
                    break

                # We don't want to remove stars that are simply on the edge of
                # distributions because if our initial guess was very wrong, we
                # could just be removing the weakest or strongest lines.

                # Instead we will remove lines that are discrepant from the line
                # fits.

                # Outliers in excitation potential vs abundance:
                x = atomic_transitions_rec["excitation_potential"][use_lines]
                y = atomic_transitions_rec["abundance"][use_lines]
                line = excitation_slope[0] * x + excitation_slope[1]

                differences = np.abs(line - y)
                excitation_sigma = differences/np.std(differences)

                # Outliers in reduced equivalent width vs abundance:
                x = reduced_equivalent_width[use_lines]
                y = atomic_transitions_rec["abundance"][use_lines]
                line = line_strength_slope[0] * x + line_strength_slope[1]

                differences = np.abs(line - y)
                line_strength_sigma = differences/np.std(differences)

                # Update the is_outliers mask
                atomic_transitions_rec["is_outlier"][use_lines] = \
                    (excitation_sigma > outlier_limit) | \
                    (line_strength_sigma > outlier_limit)

                outliers_removed = True
                continue # to re-fit the lines

        """
        import matplotlib.pyplot as plt

        # Show which was used and which wasn't
        for profile, atomic_transition in zip(profiles, atomic_transitions_rec):
            if profile[3] is None: continue

            # Find this in the atomic_transitions

            # Show  whether is an outlier/filtered.

            fig, ax = plt.subplots()
            used = atomic_transition["is_filtered"].astype(bool) \
                * ~atomic_transition["is_outlier"].astype(bool)
        
            profile_spectra = profile[3]
            ax.plot(profile_spectra["data_spectrum"].disp, profile_spectra["data_spectrum"].flux, "k")

            # fitted_spectrum, data_spectrum, continuum_spectrum, blending_spectrum_unsmoothed, blending_spectrum_smoothed
            ax.plot(profile_spectra["continuum_spectrum"].disp,
                profile_spectra["continuum_spectrum"].flux, c="#666666")
            if profile_spectra["blending_spectrum_unsmoothed"] is not None:
                ax.plot(profile_spectra["blending_spectrum_unsmoothed"].disp,
                    profile_spectra["blending_spectrum_unsmoothed"].flux, 'b:')

            if profile_spectra["blending_spectrum_smoothed"] is not None:
                ax.plot(profile_spectra["blending_spectrum_smoothed"].disp,
                    profile_spectra["blending_spectrum_smoothed"].flux, 'b')

            ax.plot(profile_spectra["fitted_spectrum"].disp, profile_spectra["fitted_spectrum"].flux, "rg"[used])

            ax.axvline(profile[0].get("wavelength", atomic_transition.wavelength), linestyle="-", c="#666666")
            ax.set_xlim(profile_spectra["data_spectrum"].disp[0], profile_spectra["data_spectrum"].disp[-1])
            ax.set_xticklabels(["{0:.2f}".format(e) for e in ax.get_xticks()])

            ax.set_title("EW {0:.1f} mA, filtered: {1}, outlier: {2}, used: {3}"
                .format(atomic_transition["equivalent_width"], atomic_transition["is_filtered"],
                    atomic_transition["is_outlier"], ["No", "Yes"][used]))

            filename = "fig-{0:.1f}.png".format(atomic_transition["wavelength"])
            fig.savefig(filename)

            plt.close("all")
            print("created {0}".format(filename))


        fig, ax = plt.subplots(2)

        # Show the data
        outlier = atomic_transitions_rec["is_outlier"].astype(bool)
        ax[0].scatter(atomic_transitions_rec["excitation_potential"][~outlier],
            atomic_transitions_rec["abundance"][~outlier], facecolor="k")
        ax[1].scatter(reduced_equivalent_width[~outlier],
            atomic_transitions_rec["abundance"][~outlier], facecolor="k")

        ax[0].scatter(atomic_transitions_rec["excitation_potential"][outlier],
            atomic_transitions_rec["abundance"][outlier], facecolor="r")
        ax[1].scatter(reduced_equivalent_width[outlier],
            atomic_transitions_rec["abundance"][outlier], facecolor="r")

        # Show the initial state before outliers were removed
        if outliers_removed:
            x = np.array(ax[0].get_xlim())
            m, b = initial_state[0][:2]
            ax[0].plot(x, x * m + b, 'r')

            x = np.array(ax[1].get_xlim())
            m, b = initial_state[3][:2]
            ax[1].plot(x, x * m + b, 'r')

        # Show the final state
        x = np.array(ax[0].get_xlim())
        m, b = final_state[0][:2]
        ax[0].plot(x, x * m + b, 'k')

        x = np.array(ax[1].get_xlim())
        m, b = final_state[3][:2]
        ax[1].plot(x, x * m + b, 'k')

        print("state is ", state)
        fig.savefig("state.png")

        print("Outliers are")
        for each in atomic_transitions_rec[outlier]:
            print(each["wavelength"], each["species"])

        plt.close("all")
        """

        if full_output:
            return (state, atomic_transitions_rec, atomic_transitions)
        return state


def wavelengths_in_data(wavelengths, data):
    orders = -np.ones(len(wavelengths), dtype=int)
    for i, spectrum in enumerate(data):
        orders[(spectrum.disp[-1] >= wavelengths) \
            * (wavelengths >= spectrum.disp[0])] = i
    return orders


def minimum_pixel_sampling(data, wavelength):
    """
    Find the minimum pixel size in the data at the given wavelength.
    """

    pixel_size = np.nan
    channel_indices = []
    for i, spectrum in enumerate(data):
        if spectrum.disp[-1] >= wavelength >= spectrum.disp[0]:
            channel_indices.append(i)

            index = spectrum.disp.searchsorted(wavelength)

            diff = np.diff(spectrum.disp)
            if pixel_size > diff[index] or not np.isfinite(pixel_size):
                pixel_size = diff[index]

    if len(channel_indices) == 0 and not np.isfinite(pixel_size):
        raise ValueError("cannot find wavelength {0:.2f} in any data channel"\
            .format(wavelength))

    return (pixel_size, channel_indices)

